"""Page alignment.

The failure mode this module exists to prevent: someone inserts one page near
the front of a 20,000 page document, and a naive page-1-vs-page-1 comparison
reports 19,999 changed pages.  Every serious comparison tool aligns pages
first; this is that step.

Strategy, in order of cost:

1. Fast path -- if both documents have the same length and every fingerprint
   matches positionally, alignment is the identity.
2. Patience anchoring -- fingerprints that occur exactly once on each side are
   unambiguous anchors.  The longest increasing subsequence of those anchors
   gives a skeleton in O(n log n).
3. Gap filling -- the runs between anchors are small, so they get a proper
   Needleman-Wunsch alignment scored on simhash similarity.  Oversized gaps
   fall back to positional pairing rather than burning O(n*m).
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter

from .extract import simhash_similarity
from .models import KIND_ADDED, KIND_MATCHED, KIND_REMOVED

# Total DP cells a single gap may use.  With a banded alignment this is a
# width budget rather than a hard ceiling on gap length, so even a wholly
# unanchored 20,000-page run still gets a real alignment.
_MAX_DP_CELLS = 6_000_000

# Band half-widths.  A gap's optimal path stays within the accumulated
# insert/delete imbalance of the diagonal, which in practice is tiny.
_MIN_BAND = 48
_MAX_BAND = 1024

# Cost of leaving a page unmatched in the DP.  Tuned so that two pages have to
# be genuinely similar (better than ~0.55 by default) to beat skipping both.
_GAP_PENALTY = 0.55

# Pages sampled per document to estimate which tokens are boilerplate.
_BACKGROUND_SAMPLE = 150


def positional_pairs(n_a: int, n_b: int) -> list[tuple[int | None, int | None, str]]:
    """Index-for-index pairing; the behaviour of the old engine."""
    pairs: list[tuple[int | None, int | None, str]] = []
    for i in range(min(n_a, n_b)):
        pairs.append((i, i, KIND_MATCHED))
    for i in range(n_b, n_a):
        pairs.append((i, None, KIND_REMOVED))
    for i in range(n_a, n_b):
        pairs.append((None, i, KIND_ADDED))
    return pairs


def _longest_increasing_subsequence(values: list[int]) -> list[int]:
    """Return indices into ``values`` forming the longest increasing run."""
    if not values:
        return []
    tails: list[int] = []          # tails[k] = index of smallest tail of len k+1
    prev: list[int] = [-1] * len(values)
    tail_vals: list[int] = []
    for i, v in enumerate(values):
        pos = bisect_left(tail_vals, v)
        if pos == len(tail_vals):
            tail_vals.append(v)
            tails.append(i)
        else:
            tail_vals[pos] = v
            tails[pos] = i
        prev[i] = tails[pos - 1] if pos > 0 else -1
    out: list[int] = []
    k = tails[-1]
    while k != -1:
        out.append(k)
        k = prev[k]
    out.reverse()
    return out


def _anchor_skeleton(hashes_a: list[int], hashes_b: list[int],
                     lo_a: int, hi_a: int,
                     lo_b: int, hi_b: int) -> list[tuple[int, int]]:
    """Unique-on-both-sides fingerprints within the given windows."""
    count_a = Counter(hashes_a[lo_a:hi_a])
    count_b = Counter(hashes_b[lo_b:hi_b])
    pos_b: dict[int, int] = {}
    for j in range(lo_b, hi_b):
        h = hashes_b[j]
        if count_a.get(h) == 1 and count_b.get(h) == 1:
            pos_b[h] = j

    candidates: list[tuple[int, int]] = []
    for i in range(lo_a, hi_a):
        h = hashes_a[i]
        j = pos_b.get(h)
        if j is not None and count_a.get(h) == 1:
            candidates.append((i, j))

    if not candidates:
        return []
    keep = _longest_increasing_subsequence([j for _, j in candidates])
    return [candidates[k] for k in keep]


def _align_gap(sims_a: list[int], sims_b: list[int],
               counts_a: list[int], counts_b: list[int],
               lo_a: int, hi_a: int, lo_b: int, hi_b: int,
               min_similarity: float) -> list[tuple[int | None, int | None, str]]:
    """Align a run with no anchors, using simhash similarity."""
    n = hi_a - lo_a
    m = hi_b - lo_b
    if n <= 0 and m <= 0:
        return []
    if n <= 0:
        return [(None, j, KIND_ADDED) for j in range(lo_b, hi_b)]
    if m <= 0:
        return [(i, None, KIND_REMOVED) for i in range(lo_a, hi_a)]

    def score(i: int, j: int) -> float:
        s = simhash_similarity(sims_a[lo_a + i], sims_b[lo_b + j])
        ca, cb = counts_a[lo_a + i], counts_b[lo_b + j]
        if ca == 0 and cb == 0:
            # Two blank pages: treat as a confident match, simhash says nothing.
            return 1.0
        if s < min_similarity:
            return -1.0
        return s

    # A gap's alignment stays close to the diagonal: within an unanchored run,
    # A[i] can only pair with B[j] if the accumulated insert/delete imbalance
    # reaches |i - j|.  Restricting the DP to a band around the diagonal turns
    # an O(n*m) problem into O(n*band), which is what lets a 3,000-page
    # unanchored run align at all instead of falling back to positional.
    band = _band_for(n, m)
    return _banded_align(n, m, lo_a, lo_b, score, band)


def _band_for(n: int, m: int) -> int:
    """Half-width of the DP band, widened for very lopsided gaps."""
    needed = abs(n - m) + _MIN_BAND
    budget = max(1, _MAX_DP_CELLS // max(1, max(n, m)))
    return max(_MIN_BAND, min(needed, budget, _MAX_BAND))


def _banded_align(n: int, m: int, lo_a: int, lo_b: int, score,
                  band: int) -> list[tuple[int | None, int | None, str]]:
    """Needleman-Wunsch restricted to |i - j| <= band.

    Scores roll through two rows; the traceback is a compact per-row bytearray
    covering only the band, so a 20,000 x 20,000 gap costs megabytes rather
    than the gigabytes a dense matrix would need.
    """
    width = 2 * band + 1
    neg_inf = float("-inf")

    def cols(i: int) -> tuple[int, int]:
        return max(0, i - band), min(m, i + band)

    # bt codes: 0 diagonal (match), 1 up (removed), 2 left (added), 3 origin
    bt = [bytearray(width) for _ in range(n + 1)]

    lo, hi = cols(0)
    prev = [neg_inf] * (hi - lo + 1)
    prev_lo = lo
    prev[0] = 0.0
    bt[0][0 - 0 + band] = 3
    for j in range(lo + 1, hi + 1):
        prev[j - prev_lo] = prev[j - 1 - prev_lo] - _GAP_PENALTY
        bt[0][j - 0 + band] = 2

    for i in range(1, n + 1):
        lo, hi = cols(i)
        cur = [neg_inf] * (hi - lo + 1)
        brow = bt[i]
        for j in range(lo, hi + 1):
            best = neg_inf
            move = 3
            if j - 1 >= prev_lo and (j - 1) <= prev_lo + len(prev) - 1:
                diag = prev[j - 1 - prev_lo]
                if diag > neg_inf:
                    diag += score(i - 1, j - 1)
                    if diag > best:
                        best, move = diag, 0
            if prev_lo <= j <= prev_lo + len(prev) - 1:
                up = prev[j - prev_lo]
                if up > neg_inf:
                    up -= _GAP_PENALTY
                    if up > best:
                        best, move = up, 1
            if j - 1 >= lo:
                left = cur[j - 1 - lo]
                if left > neg_inf:
                    left -= _GAP_PENALTY
                    if left > best:
                        best, move = left, 2
            cur[j - lo] = best
            brow[j - i + band] = move
        prev, prev_lo = cur, lo

    out: list[tuple[int | None, int | None, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        offset = j - i + band
        move = bt[i][offset] if 0 <= offset < width else (1 if i > 0 else 2)
        if move == 0 and i > 0 and j > 0:
            out.append((lo_a + i - 1, lo_b + j - 1, KIND_MATCHED))
            i -= 1
            j -= 1
        elif move == 1 and i > 0:
            out.append((lo_a + i - 1, None, KIND_REMOVED))
            i -= 1
        elif move == 2 and j > 0:
            out.append((None, lo_b + j - 1, KIND_ADDED))
            j -= 1
        elif i > 0:
            out.append((lo_a + i - 1, None, KIND_REMOVED))
            i -= 1
        else:
            out.append((None, lo_b + j - 1, KIND_ADDED))
            j -= 1
    out.reverse()
    return out


def align_documents(exact_a: list[int], exact_b: list[int],
                    sims_a: list[int], sims_b: list[int],
                    counts_a: list[int], counts_b: list[int],
                    min_similarity: float = 0.55,
                    enabled: bool = True,
                    tokens_for=None) -> list[tuple[int | None, int | None, str]]:
    """Produce the ordered list of page pairs for the whole comparison."""
    n_a, n_b = len(exact_a), len(exact_b)
    if not enabled:
        return positional_pairs(n_a, n_b)
    if n_a == 0 or n_b == 0:
        return positional_pairs(n_a, n_b)

    # Fast path: identical structure.  Very common when someone re-exports a
    # document with a handful of edits, and it skips all the machinery below.
    if n_a == n_b and exact_a == exact_b:
        return [(i, i, KIND_MATCHED) for i in range(n_a)]

    anchors = _anchor_skeleton(exact_a, exact_b, 0, n_a, 0, n_b)

    pairs: list[tuple[int | None, int | None, str]] = []
    prev_a = prev_b = 0
    for a_idx, b_idx in anchors:
        pairs.extend(_align_gap(sims_a, sims_b, counts_a, counts_b,
                                prev_a, a_idx, prev_b, b_idx, min_similarity))
        pairs.append((a_idx, b_idx, KIND_MATCHED))
        prev_a, prev_b = a_idx + 1, b_idx + 1
    pairs.extend(_align_gap(sims_a, sims_b, counts_a, counts_b,
                            prev_a, n_a, prev_b, n_b, min_similarity))

    # A monotonic mapping cannot express a page that moved past its
    # neighbours; recover those before calling anything removed.
    pairs, _rescued = rescue_reordered(pairs, exact_a, exact_b, sims_a, sims_b,
                                       tokens_for=tokens_for)
    return pairs


def rescue_reordered(pairs, exact_a: list[int], exact_b: list[int],
                     sims_a: list[int], sims_b: list[int],
                     tokens_for=None, min_score: float = 0.35,
                     max_candidates: int = 4_000_000):
    """Pair up leftover pages that were reordered rather than removed.

    Everything above this point is order-preserving: patience anchoring and
    the banded DP both produce a monotonic mapping, because that is what a
    document edited in place looks like. Batch-generated documents are not
    like that. Regenerate a run of 1,000 trade confirmations and the same
    confirmation can land at a completely different index -- page 991 in one
    file, page 468 in the other. A monotonic alignment cannot express that
    crossing, so it has no choice but to call one page removed and the other
    added, and the reviewer is told content vanished when it plainly did not.

    This pass takes only the pages the monotonic phase gave up on and matches
    them to each other regardless of position: identical fingerprints first,
    then perceptual similarity. Anything still unpaired stays genuinely
    added or removed.
    """
    leftover_a = [p[0] for p in pairs if p[2] == KIND_REMOVED and p[0] is not None]
    leftover_b = [p[1] for p in pairs if p[2] == KIND_ADDED and p[1] is not None]
    if not leftover_a or not leftover_b:
        return pairs, 0

    matches: dict[int, int] = {}

    # Pass 1: identical content, wherever it ended up.
    by_hash: dict[int, list[int]] = {}
    for j in leftover_b:
        by_hash.setdefault(exact_b[j], []).append(j)
    still_a: list[int] = []
    for i in leftover_a:
        pool = by_hash.get(exact_a[i])
        if pool:
            matches[i] = pool.pop(0)
        else:
            still_a.append(i)
    taken = set(matches.values())
    still_b = [j for j in leftover_b if j not in taken]

    # Pass 2: near-identical content, when a token supplier is available.
    #
    # Simhash is deliberately NOT used here. On batch-generated documents --
    # a run of trade confirmations, invoices, statements -- every page shares
    # the same letterhead, addresses and legal boilerplate, so a whole-bag
    # similarity is near 1.0 between pages that have nothing to do with each
    # other. Matching on that produced confident pairings of unrelated pages,
    # which is worse than admitting the page was not found.
    #
    # What actually identifies such a page is its rare tokens: the trade
    # number, the account, the CUSIP. ``tokens_for`` supplies them and the
    # score weights each shared token by how rare it is across the candidate
    # set, so boilerplate contributes almost nothing.
    if still_a and still_b and tokens_for is not None \
            and len(still_a) * len(still_b) <= max_candidates:
        # Sample pages from across both documents so token rarity is judged
        # against the document as a whole, not against ten similar leftovers.
        background = []
        for side, total in ((0, len(exact_a)), (1, len(exact_b))):
            if total:
                step = max(1, total // _BACKGROUND_SAMPLE)
                background.extend(tokens_for(side, k)
                                  for k in range(0, total, step))
        matches.update(_match_by_rare_tokens(still_a, still_b, tokens_for,
                                             background, min_score))

    if not matches:
        return pairs, 0

    paired_b = set(matches.values())
    rebuilt: list[tuple[int | None, int | None, str]] = []
    for a, b, kind in pairs:
        if kind == KIND_REMOVED and a in matches:
            rebuilt.append((a, matches[a], KIND_MATCHED))
        elif kind == KIND_ADDED and b in paired_b:
            continue                      # now carried by its A partner
        else:
            rebuilt.append((a, b, kind))
    return rebuilt, len(matches)


# A token on this many pages or fewer (within the sampled background) is
# treated as identifying rather than boilerplate.
_RARE_DF = 5
# A rescue needs at least this many shared identifying tokens, and this
# fraction of the page's own identifying tokens, before it is believed.
_MIN_SHARED_RARE = 3
_MIN_RARE_FRACTION = 0.6


def _match_by_rare_tokens(a_pages, b_pages, tokens_for, background,
                          min_score: float):
    """Pair pages on identifying tokens, and refuse to guess.

    Weighting by document frequency is only meaningful against a realistic
    background. Computing it from the leftover pages alone was the mistake
    that made this pass useless: on a run of trade confirmations those pages
    share hundreds of boilerplate tokens, every one of them looks "rare"
    within a sample of ten, and the matcher confidently paired pages that had
    nothing in common. Document frequency is therefore measured against a
    sample of pages from across the document.

    The bar to declare a match is deliberately high. A page whose identifying
    tokens are absent from the other document has genuinely been removed, and
    saying so is more useful than inventing a partner for it.
    """
    side_a = {i: tokens_for(0, i) for i in a_pages}
    side_b = {j: tokens_for(1, j) for j in b_pages}

    df: dict[str, int] = {}
    for bag in background:
        for tok in bag:
            df[tok] = df.get(tok, 0) + 1

    def rare(bag: set) -> set:
        return {t for t in bag if df.get(t, 0) <= _RARE_DF}

    rare_a = {i: rare(bag) for i, bag in side_a.items()}
    rare_b = {j: rare(bag) for j, bag in side_b.items()}

    scored: list[tuple[float, int, int]] = []
    for i, ra in rare_a.items():
        if len(ra) < _MIN_SHARED_RARE:
            continue                      # nothing distinctive to match on
        for j, rb in rare_b.items():
            shared = ra & rb
            if len(shared) < _MIN_SHARED_RARE:
                continue
            fraction = len(shared) / max(len(ra), len(rb))
            if fraction >= max(min_score, _MIN_RARE_FRACTION):
                scored.append((fraction, i, j))

    scored.sort(reverse=True)
    out: dict[int, int] = {}
    used_a: set[int] = set()
    used_b: set[int] = set()
    for _score, i, j in scored:
        if i in used_a or j in used_b:
            continue
        out[i] = j
        used_a.add(i)
        used_b.add(j)
    return out


def alignment_stats(pairs) -> dict:
    matched = sum(1 for a, b, k in pairs if k == KIND_MATCHED)
    added = sum(1 for a, b, k in pairs if k == KIND_ADDED)
    removed = sum(1 for a, b, k in pairs if k == KIND_REMOVED)
    shifted = sum(1 for a, b, k in pairs
                  if k == KIND_MATCHED and a is not None and b is not None and a != b)
    return {"matched": matched, "added": added,
            "removed": removed, "shifted": shifted}
