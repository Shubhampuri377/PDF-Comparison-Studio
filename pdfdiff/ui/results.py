"""Results workspace: summary bar, page navigator, viewer, inspector."""
from __future__ import annotations

import os

from PySide6.QtCore import (QItemSelection, QItemSelectionModel, QSize, Qt,
                            Signal)
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QSlider,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ..changes import group_changes
from ..ignore import (IgnoreRule, IgnoreSet, SCOPE_ALL, SCOPE_EVEN,
                      SCOPE_ODD, SCOPE_PAGES, SIDE_BOTH, SIDE_LEFT, SIDE_RIGHT)
from ..models import (ALL_CATEGORIES, CATEGORY_LABELS, IMAGE_CATEGORIES,
                      KIND_ADDED, KIND_REMOVED, PageStat, Summary,
                      TEXT_CATEGORIES)
from ..store import (FILTER_ADDED, FILTER_ALL, FILTER_CHANGED, FILTER_ERROR,
                     FILTER_IDENTICAL, FILTER_IMAGE, FILTER_REMOVED,
                     FILTER_TEXT, ResultStore)
from .pagelist import (PageListDelegate, PageListModel, PageListView, StatRole,
                       ThumbRole)
from .theme import CATEGORY_HEX, Palette, rect_select_icon
from .viewer import (MODE_LEFT, MODE_OVERLAY, MODE_RIGHT, MODE_SPLIT,
                     ComparisonViewer)
from .widgets import (Card, LegendChip, Pill, SegmentedControl, SimilarityBar,
                      StatTile, hline, label, section)


class ResultsPage(QWidget):
    """Everything the user does after a comparison finishes."""

    export_requested = Signal()
    new_comparison_requested = Signal()
    rediff_requested = Signal(list)        # list of ignore rule dicts
    settings_requested = Signal()

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.store: ResultStore | None = None
        self.summary: Summary | None = None
        self._render_token = 0
        self._current_row = -1
        self._current_stat: PageStat | None = None
        self._current_boxes: list = []
        self._ignore_rules: list[IgnoreRule] = []
        self._render_service = None
        self._connected_service = None
        self._pending_scale = 1.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_summary_bar())
        root.addWidget(self._build_body(), 1)

        self._install_shortcuts()

    # ------------------------------------------------------------------
    # Summary bar
    # ------------------------------------------------------------------
    def _build_summary_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(16, 11, 16, 11)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(9)

        self.tile_total = StatTile("Pages", "—")
        self.tile_identical = StatTile("Identical", "—", self.p.ok)
        self.tile_changed = StatTile("Changed", "—", self.p.warn)
        self.tile_added = StatTile("Added", "—", self.p.ok)
        self.tile_removed = StatTile("Removed", "—", self.p.danger)
        self.tile_changes = StatTile("Total changes", "—")
        self.tile_match = StatTile("Match rate", "—")
        for t in (self.tile_total, self.tile_identical, self.tile_changed,
                  self.tile_added, self.tile_removed, self.tile_changes,
                  self.tile_match):
            top.addWidget(t)
        top.addStretch(1)

        self.new_btn = QPushButton("New comparison")
        self.new_btn.setProperty("flat", True)
        self.new_btn.clicked.connect(self.new_comparison_requested.emit)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setProperty("flat", True)
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        self.export_btn = QPushButton("Export report")
        self.export_btn.setProperty("accent", True)
        self.export_btn.clicked.connect(self.export_requested.emit)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(self.new_btn)
        row.addWidget(self.settings_btn)
        row.addWidget(self.export_btn)
        side.addLayout(row)
        side.addStretch(1)
        top.addLayout(side)

        lay.addLayout(top)
        return bar

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------
    def _build_body(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_rail())
        splitter.addWidget(self._build_centre())
        splitter.addWidget(self._build_right_rail())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([312, 1100, 322])
        self._splitter = splitter
        return splitter

    # -- left ----------------------------------------------------------
    def _build_left_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("Rail")
        rail.setMinimumWidth(240)
        rail.setMaximumWidth(430)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(12, 12, 10, 12)
        lay.setSpacing(9)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Jump to page number…")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._jump_to_typed)
        lay.addWidget(self.search)

        self.filter_control = SegmentedControl([
            (FILTER_CHANGED, "Changed"),
            (FILTER_ALL, "All"),
            (FILTER_TEXT, "Text"),
            (FILTER_IMAGE, "Images"),
        ])
        self.filter_control.changed.connect(self._apply_filter)
        lay.addWidget(self.filter_control)

        extra = QHBoxLayout()
        extra.setSpacing(5)
        self.filter_extra = QComboBox()
        self.filter_extra.addItem("No extra filter", "")
        self.filter_extra.addItem("Added pages only", FILTER_ADDED)
        self.filter_extra.addItem("Removed pages only", FILTER_REMOVED)
        self.filter_extra.addItem("Identical only", FILTER_IDENTICAL)
        self.filter_extra.addItem("Errors only", FILTER_ERROR)
        self.filter_extra.currentIndexChanged.connect(
            lambda _i: self._apply_filter(self.filter_extra.currentData()
                                          or self.filter_control.current()))
        extra.addWidget(self.filter_extra, 1)
        lay.addLayout(extra)

        self.list_view = PageListView()
        self.list_model = PageListModel(self)
        self.list_view.setModel(self.list_model)
        self.list_delegate = PageListDelegate(self.p, self)
        self.list_view.setItemDelegate(self.list_delegate)
        self.list_view.selectionModel().selectionChanged.connect(
            self._on_selection)
        self.list_view.visible_changed.connect(self._on_visible_rows)
        lay.addWidget(self.list_view, 1)

        self.list_status = label("", "Faint")
        lay.addWidget(self.list_status)
        return rail

    # -- centre --------------------------------------------------------
    def _build_centre(self) -> QWidget:
        centre = QWidget()
        lay = QVBoxLayout(centre)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("TopBar")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(12, 7, 12, 7)
        tl.setSpacing(7)

        self.prev_change_btn = QPushButton("◀ Prev change")
        self.prev_change_btn.setProperty("flat", True)
        self.prev_change_btn.clicked.connect(lambda: self.step_change(-1))
        self.next_change_btn = QPushButton("Next change ▶")
        self.next_change_btn.setProperty("flat", True)
        self.next_change_btn.clicked.connect(lambda: self.step_change(1))
        tl.addWidget(self.prev_change_btn)
        tl.addWidget(self.next_change_btn)

        sep = QFrame()
        sep.setObjectName("Separator")
        sep.setFixedWidth(1)
        tl.addWidget(sep)

        self.page_pill = Pill("—", self.p.text_dim)
        tl.addWidget(self.page_pill)

        sep2 = QFrame()
        sep2.setObjectName("Separator")
        sep2.setFixedWidth(1)
        tl.addWidget(sep2)

        # The marquee tool lived only inside a right-rail tab and nobody found
        # it. It belongs on the main toolbar next to the other view controls.
        self.exclude_btn = QPushButton("  Exclude area")
        self.exclude_btn.setCheckable(True)
        self.exclude_btn.setProperty("flat", True)
        self.exclude_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exclude_btn.setIconSize(QSize(15, 15))
        self.exclude_btn.setIcon(rect_select_icon(self.p.text_dim, 15))
        self.exclude_btn.setToolTip(
            "Drag a rectangle over the page to exclude that area from the "
            "comparison  (E)")
        self.exclude_btn.toggled.connect(self._on_draw_toggled)
        tl.addWidget(self.exclude_btn)

        tl.addStretch(1)

        self.mode_control = SegmentedControl([
            (MODE_SPLIT, "Side by side"),
            (MODE_OVERLAY, "Overlay"),
            (MODE_LEFT, "A only"),
            (MODE_RIGHT, "B only"),
        ])
        self.mode_control.changed.connect(self._on_mode)
        tl.addWidget(self.mode_control)

        self.overlay_slider = QSlider(Qt.Orientation.Horizontal)
        self.overlay_slider.setRange(0, 100)
        self.overlay_slider.setValue(50)
        self.overlay_slider.setFixedWidth(96)
        self.overlay_slider.setVisible(False)
        self.overlay_slider.valueChanged.connect(
            lambda v: self.viewer.set_overlay_opacity(v / 100.0))
        tl.addWidget(self.overlay_slider)

        tl.addStretch(1)

        self.sync_check = QCheckBox("Sync")
        self.sync_check.setChecked(True)
        self.sync_check.setToolTip("Lock panning and zoom across both pages")
        self.sync_check.toggled.connect(lambda on: self.viewer.set_sync(on))
        tl.addWidget(self.sync_check)

        for text, slot, tip in (("−", lambda: self.viewer.zoom_by(1 / 1.25),
                                 "Zoom out"),
                                ("+", lambda: self.viewer.zoom_by(1.25),
                                 "Zoom in")):
            btn = QPushButton(text)
            btn.setProperty("flat", True)
            btn.setFixedWidth(30)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            tl.addWidget(btn)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setFixedWidth(46)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setStyleSheet(f"color:{self.p.text_dim};")
        tl.addWidget(self.zoom_label)

        fit_page = QPushButton("Fit")
        fit_page.setProperty("flat", True)
        fit_page.clicked.connect(lambda: self.viewer.fit_page())
        fit_width = QPushButton("Width")
        fit_width.setProperty("flat", True)
        fit_width.clicked.connect(lambda: self.viewer.fit_width())
        tl.addWidget(fit_page)
        tl.addWidget(fit_width)

        lay.addWidget(toolbar)

        self.viewer = ComparisonViewer(self.p)
        self.viewer.zoom_changed.connect(self._on_zoom)
        self.viewer.render_needed.connect(self._on_render_scale_needed)
        self.viewer.region_drawn.connect(self._on_region_drawn)
        lay.addWidget(self.viewer, 1)
        return centre

    # -- right ---------------------------------------------------------
    def _build_right_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("RailRight")
        rail.setMinimumWidth(260)
        rail.setMaximumWidth(430)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(9)

        tabs = QTabWidget()
        tabs.addTab(self._build_inspector(), "Changes")
        tabs.addTab(self._build_ignore_panel(), "Ignore areas")
        lay.addWidget(tabs, 1)
        self._right_tabs = tabs
        return rail

    def _build_inspector(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(10)

        self.page_title = label("No page selected", "H3")
        lay.addWidget(self.page_title)

        self.page_flags = label("", "Faint", wrap=True)
        lay.addWidget(self.page_flags)

        sim_row = QVBoxLayout()
        sim_row.setSpacing(4)
        sim_head = QHBoxLayout()
        sim_head.addWidget(label("Similarity", "Faint"))
        sim_head.addStretch(1)
        self.sim_value = label("—", "Faint")
        sim_head.addWidget(self.sim_value)
        sim_row.addLayout(sim_head)
        self.sim_bar = SimilarityBar(self.p)
        sim_row.addWidget(self.sim_bar)
        lay.addLayout(sim_row)

        lay.addWidget(hline(self.p))
        lay.addWidget(section("Highlight layers"))

        self.legend_chips: dict[str, LegendChip] = {}
        for cat in ALL_CATEGORIES:
            chip = LegendChip(cat, CATEGORY_LABELS[cat])
            chip.toggled.connect(self._on_legend_toggle)
            self.legend_chips[cat] = chip
            lay.addWidget(chip)

        lay.addWidget(hline(self.p))
        head = QHBoxLayout()
        head.addWidget(section("Changes on this page"))
        head.addStretch(1)
        lay.addLayout(head)

        self.change_list = QListWidget()
        self.change_list.setFrameShape(QFrame.Shape.NoFrame)
        # Change descriptions are whole phrases now, so they need to wrap
        # rather than run off the edge behind a horizontal scrollbar.
        self.change_list.setWordWrap(True)
        self.change_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.change_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.change_list.itemClicked.connect(self._on_change_clicked)
        lay.addWidget(self.change_list, 1)
        return w

    def _build_ignore_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(9)

        lay.addWidget(label(
            "Exclude parts of the page from the comparison — headers, "
            "footers, watermarks, revision stamps.", "Faint", wrap=True))

        self.draw_btn = QPushButton("  Draw exclusion rectangle")
        self.draw_btn.setCheckable(True)
        self.draw_btn.setIconSize(QSize(15, 15))
        self.draw_btn.setIcon(rect_select_icon(self.p.text_dim, 15))
        self.draw_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.draw_btn.setToolTip("Shortcut: E")
        self.draw_btn.toggled.connect(self._on_draw_toggled)
        lay.addWidget(self.draw_btn)

        self.draw_hint = label(
            "Drag a rectangle over either page. Anything inside it is "
            "excluded from the comparison. Press Esc to cancel.",
            "Faint", wrap=True)
        self.draw_hint.setVisible(False)
        lay.addWidget(self.draw_hint)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        top_btn = QPushButton("Header band")
        top_btn.setProperty("flat", True)
        top_btn.clicked.connect(lambda: self._add_rule(
            IgnoreRule(0, 0, 1, 0.08, label="Header band")))
        bottom_btn = QPushButton("Footer band")
        bottom_btn.setProperty("flat", True)
        bottom_btn.clicked.connect(lambda: self._add_rule(
            IgnoreRule(0, 0.92, 1, 1, label="Footer band")))
        quick.addWidget(top_btn)
        quick.addWidget(bottom_btn)
        lay.addLayout(quick)

        lay.addWidget(hline(self.p))
        lay.addWidget(section("Active regions"))

        self.rules_list = QListWidget()
        self.rules_list.setFrameShape(QFrame.Shape.NoFrame)
        self.rules_list.currentRowChanged.connect(self._on_rule_selected)
        lay.addWidget(self.rules_list, 1)

        edit = QWidget()
        el = QVBoxLayout(edit)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(6)

        scope_row = QHBoxLayout()
        scope_row.addWidget(label("Applies to", "Faint"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All pages", SCOPE_ALL)
        self.scope_combo.addItem("Specific pages", SCOPE_PAGES)
        self.scope_combo.addItem("Odd pages", SCOPE_ODD)
        self.scope_combo.addItem("Even pages", SCOPE_EVEN)
        self.scope_combo.currentIndexChanged.connect(self._update_selected_rule)
        scope_row.addWidget(self.scope_combo, 1)
        el.addLayout(scope_row)

        self.pages_edit = QLineEdit()
        self.pages_edit.setPlaceholderText("e.g. 1-5, 9, 200-")
        self.pages_edit.setVisible(False)
        self.pages_edit.editingFinished.connect(self._update_selected_rule)
        el.addWidget(self.pages_edit)

        side_row = QHBoxLayout()
        side_row.addWidget(label("Document", "Faint"))
        self.side_combo = QComboBox()
        self.side_combo.addItem("Both", SIDE_BOTH)
        self.side_combo.addItem("A only", SIDE_LEFT)
        self.side_combo.addItem("B only", SIDE_RIGHT)
        self.side_combo.currentIndexChanged.connect(self._update_selected_rule)
        side_row.addWidget(self.side_combo, 1)
        el.addLayout(side_row)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.remove_rule_btn = QPushButton("Remove")
        self.remove_rule_btn.setProperty("danger", True)
        self.remove_rule_btn.clicked.connect(self._remove_rule)
        clear_btn = QPushButton("Clear all")
        clear_btn.setProperty("flat", True)
        clear_btn.clicked.connect(self._clear_rules)
        btns.addWidget(self.remove_rule_btn)
        btns.addWidget(clear_btn)
        btns.addStretch(1)
        el.addLayout(btns)
        lay.addWidget(edit)
        self._rule_editor = edit
        edit.setEnabled(False)

        self.apply_btn = QPushButton("Apply and re-compare")
        self.apply_btn.setProperty("accent", True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply_rules)
        lay.addWidget(self.apply_btn)

        self.rules_hint = label(
            "Re-comparing with new ignore areas does not re-read the PDFs, "
            "so it finishes in a fraction of the original time.",
            "Faint", wrap=True)
        lay.addWidget(self.rules_hint)
        return w

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------
    def _install_shortcuts(self):
        for keys, slot in (
                ("Ctrl+Right", lambda: self.step_page(1)),
                ("Ctrl+Left", lambda: self.step_page(-1)),
                ("F3", lambda: self.step_change(1)),
                ("Shift+F3", lambda: self.step_change(-1)),
                ("Ctrl+=", lambda: self.viewer.zoom_by(1.25)),
                ("Ctrl++", lambda: self.viewer.zoom_by(1.25)),
                ("Ctrl+-", lambda: self.viewer.zoom_by(1 / 1.25)),
                ("Ctrl+0", lambda: self.viewer.fit_page()),
                ("Ctrl+F", lambda: self.search.setFocus()),
                ("E", lambda: self.exclude_btn.toggle()),
                ("Esc", lambda: self.exclude_btn.setChecked(False))):
            QShortcut(QKeySequence(keys), self, activated=slot)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def apply_palette(self, palette: Palette):
        self.p = palette
        for tile, colour in ((self.tile_total, None),
                             (self.tile_identical, palette.ok),
                             (self.tile_changed, palette.warn),
                             (self.tile_added, palette.ok),
                             (self.tile_removed, palette.danger),
                             (self.tile_changes, None),
                             (self.tile_match, None)):
            tile.set_accent(colour)
        self.zoom_label.setStyleSheet(f"color:{palette.text_dim};")
        on = self.exclude_btn.isChecked()
        for btn in (self.exclude_btn, self.draw_btn):
            btn.setIcon(rect_select_icon(
                palette.accent if on else palette.text_dim, 15))
        if self._current_stat is not None:
            self._update_inspector(self._current_stat)

    # ------------------------------------------------------------------
    # Data wiring
    # ------------------------------------------------------------------
    def attach(self, store: ResultStore, summary: Summary,
               render_service, name_a: str, name_b: str,
               ignore_rules: list[dict] | None = None):
        self.store = store
        self.summary = summary
        self._render_service = render_service
        self.list_model.set_store(store)
        self.viewer.set_captions(f"A — {name_a}", f"B — {name_b}")
        self._ignore_rules = [IgnoreRule.from_dict(r) for r in (ignore_rules or [])]
        self._refresh_rules_list()

        # Detach from the previous run's service before binding the new one.
        # Tracking what we connected beats blind disconnect() calls, which warn
        # noisily on the first attach when there is nothing to unhook.
        if self._connected_service is not None:
            try:
                self._connected_service.thumb_ready.disconnect(self._on_thumb_ready)
                self._connected_service.page_ready.disconnect(self._on_page_ready)
            except RuntimeError:
                pass                      # the old service was already deleted
        else:
            self.list_model.thumb_needed.connect(self._on_thumb_needed)

        render_service.thumb_ready.connect(self._on_thumb_ready)
        render_service.page_ready.connect(self._on_page_ready)
        self._connected_service = render_service

        self.update_summary(summary)
        self._apply_filter(FILTER_CHANGED
                           if summary.changed else FILTER_ALL)
        self.filter_control.set_current(
            FILTER_CHANGED if summary.changed else FILTER_ALL, emit=False)

    def update_summary(self, summary: Summary):
        self.summary = summary
        self.tile_total.set_value(f"{summary.total_pairs:,}")
        self.tile_identical.set_value(f"{summary.identical:,}")
        self.tile_changed.set_value(f"{summary.changed:,}")
        self.tile_added.set_value(f"{summary.added_pages:,}")
        self.tile_removed.set_value(f"{summary.removed_pages:,}")
        self.tile_changes.set_value(f"{summary.total_changes:,}")
        self.tile_match.set_value(f"{summary.match_rate * 100:.1f}%")
        for cat, chip in self.legend_chips.items():
            chip.set_count(summary.counts.get(cat, 0))

    def refresh_after_rediff(self, summary: Summary):
        self.update_summary(summary)
        self.list_model.invalidate()
        self._apply_filter(self.filter_control.current())

    # -- filtering -----------------------------------------------------
    def _apply_filter(self, flt: str):
        if self.store is None:
            return
        extra = self.filter_extra.currentData()
        effective = extra or flt or FILTER_ALL
        rows = self.store.pair_indices(effective)
        keep_idx = self.list_model.pair_index(self._current_row)
        self.list_model.set_rows(rows)
        self.list_status.setText(
            f"{len(rows):,} of {self.store.pair_count(FILTER_ALL):,} pages")
        if not rows:
            self.viewer.set_pages(None, None, (612, 792), (612, 792), 1.0, [],
                                  "Nothing matches this filter", "")
            self.page_title.setText("No pages match")
            self.change_list.clear()
            return
        target = self.list_model.row_of(keep_idx) if keep_idx >= 0 else 0
        self.select_row(max(0, target))

    def _jump_to_typed(self):
        text = self.search.text().strip()
        if not text or self.store is None:
            return
        try:
            wanted = int(text) - 1
        except ValueError:
            return
        # Match on the A-side page number first, then the B side.
        for row in range(self.list_model.rowCount()):
            stat = self.list_model.data(self.list_model.index(row), StatRole)
            if stat and (stat.a_pno == wanted or
                         (stat.a_pno is None and stat.b_pno == wanted)):
                self.select_row(row)
                return
        self.list_status.setText(f"Page {wanted + 1} is not in the current filter")

    # -- selection -----------------------------------------------------
    def select_row(self, row: int):
        if not (0 <= row < self.list_model.rowCount()):
            return
        index = self.list_model.index(row)
        self.list_view.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.list_view.scrollTo(index,
                                QAbstractItemView.ScrollHint.EnsureVisible)

    def step_page(self, delta: int):
        self.select_row(self._current_row + delta)

    def step_change(self, direction: int):
        """Move to the next page that has differences, in list order."""
        row = self._current_row
        count = self.list_model.rowCount()
        step = 1 if direction > 0 else -1
        row += step
        while 0 <= row < count:
            stat = self.list_model.data(self.list_model.index(row), StatRole)
            if stat and (stat.status == "changed" or stat.kind != "matched"):
                self.select_row(row)
                return
            row += step

    def _on_selection(self, selected: QItemSelection, _deselected):
        indexes = selected.indexes()
        if not indexes:
            return
        row = indexes[0].row()
        self._current_row = row
        stat = self.list_model.data(indexes[0], StatRole)
        if stat is None:
            return
        self._current_stat = stat
        self._load_page(stat)

    def _load_page(self, stat: PageStat):
        self._current_boxes = self.store.get_boxes(stat.idx) if self.store else []
        self._update_inspector(stat)
        self._update_ignore_overlay(stat)

        self._render_token += 1
        scale = self.viewer.required_render_scale()
        self._pending_scale = scale
        if self._render_service is not None:
            self._render_service.request_page(
                self._render_token, stat.idx, stat.a_pno, stat.b_pno, scale)

        self.page_pill.set_state(
            stat.label(),
            self.p.ok if stat.status == "identical" else self.p.warn)

    def _page_size(self, side: int, pno) -> tuple[float, float]:
        """Page dimensions from the store, not from the PDF.

        Ingest already recorded these, so the GUI thread never has to open a
        document just to lay out a page it is about to display.
        """
        if pno is None or self.store is None:
            return 612.0, 792.0
        geom = self.store.page_geometry(side, pno)
        if not geom:
            return 612.0, 792.0
        return float(geom[0]), float(geom[1])

    def _on_page_ready(self, token: int, _pair_idx: int, img_a, img_b):
        if token != self._render_token:
            return                       # a newer page was requested meanwhile
        stat = self._current_stat
        if stat is None:
            return
        # The render thread hands over QImage; QPixmap may only be built here,
        # on the GUI thread.
        pm_a = QPixmap.fromImage(img_a) if img_a is not None else None
        pm_b = QPixmap.fromImage(img_b) if img_b is not None else None
        size_a = self._page_size(0, stat.a_pno)
        size_b = self._page_size(1, stat.b_pno)
        self.viewer.set_pages(
            pm_a, pm_b, size_a, size_b,
            self._pending_scale, self._current_boxes,
            missing_a="This page does not exist in document A",
            missing_b="This page does not exist in document B")

    def _on_render_scale_needed(self, scale: float):
        stat = self._current_stat
        if stat is None or self._render_service is None:
            return
        self._render_token += 1
        self._pending_scale = scale
        self._render_service.request_page(
            self._render_token, stat.idx, stat.a_pno, stat.b_pno, scale)

    # -- thumbnails ----------------------------------------------------
    def _on_thumb_needed(self, generation: int, row: int, a_pno, b_pno):
        if self._render_service is not None:
            self._render_service.request_thumb(generation, row, a_pno, b_pno, 96)

    def _on_thumb_ready(self, generation: int, row: int, img):
        pm = QPixmap.fromImage(img) if img is not None else None
        self.list_model.set_thumb(generation, row, pm)

    def _on_visible_rows(self, first: int, last: int):
        # Reading the thumbnail role is what queues a render for a row, so
        # this both warms the visible window and leaves everything else alone.
        for row in range(first, last + 1):
            self.list_model.data(self.list_model.index(row), ThumbRole)

    # -- inspector -----------------------------------------------------
    def _update_inspector(self, stat: PageStat):
        self.page_title.setText(stat.label())
        flags = stat.flag_labels()
        if stat.error:
            self.page_flags.setText(stat.error)
            self.page_flags.setStyleSheet(f"color:{self.p.danger};")
        elif flags:
            self.page_flags.setText(" · ".join(flags))
            self.page_flags.setStyleSheet(f"color:{self.p.warn};")
        else:
            self.page_flags.setText("")

        self.sim_bar.set_value(stat.similarity)
        self.sim_value.setText(f"{stat.similarity * 100:.1f}%")

        self.change_list.clear()
        if stat.status == "identical":
            item = QListWidgetItem("No differences on this page")
            item.setForeground(QColor(self.p.text_faint))
            self.change_list.addItem(item)
            return

        shown = 0
        for change in group_changes(self._current_boxes):
            category = change.category
            item = QListWidgetItem(
                f"{CATEGORY_LABELS.get(category, category)}: "
                f"{change.text[:120]}")
            item.setForeground(QColor(CATEGORY_HEX.get(category, self.p.text)))
            item.setData(Qt.ItemDataRole.UserRole, change.anchor)
            self.change_list.addItem(item)
            shown += 1
            if shown >= 300:
                item = QListWidgetItem("… more changes on this page")
                item.setForeground(QColor(self.p.text_faint))
                self.change_list.addItem(item)
                break

        if not shown:
            for cat, n in sorted(stat.counts.items(), key=lambda kv: -kv[1]):
                item = QListWidgetItem(
                    f"{CATEGORY_LABELS.get(cat, cat)} × {n:,}")
                item.setForeground(QColor(CATEGORY_HEX.get(cat, self.p.text)))
                self.change_list.addItem(item)

    def _on_change_clicked(self, item: QListWidgetItem):
        box = item.data(Qt.ItemDataRole.UserRole)
        if box:
            self.viewer.focus_box(box)

    def _on_legend_toggle(self, _category: str, _on: bool):
        enabled = {c for c, chip in self.legend_chips.items() if chip.is_on()}
        self.viewer.set_enabled_categories(enabled)

    # -- viewer --------------------------------------------------------
    def _on_mode(self, mode: str):
        self.viewer.set_mode(mode)
        self.overlay_slider.setVisible(mode == MODE_OVERLAY)
        self.sync_check.setEnabled(mode == MODE_SPLIT)

    def _on_zoom(self, zoom: float):
        self.zoom_label.setText(f"{zoom * 100:.0f}%")

    # ------------------------------------------------------------------
    # Ignore rules
    # ------------------------------------------------------------------
    def _on_draw_toggled(self, on: bool):
        """Single handler for both entry points, kept in sync.

        The toolbar button and the one in the Ignore panel drive the same
        mode, so each mirrors the other's state without re-emitting.
        """
        self.viewer.set_draw_mode(on)
        for btn in (self.exclude_btn, self.draw_btn):
            if btn.isChecked() != on:
                btn.blockSignals(True)
                btn.setChecked(on)
                btn.blockSignals(False)

        self.draw_btn.setText("  Drag on the page to mark it" if on
                              else "  Draw exclusion rectangle")
        self.exclude_btn.setIcon(rect_select_icon(
            self.p.accent if on else self.p.text_dim, 15))
        self.draw_btn.setIcon(rect_select_icon(
            self.p.accent if on else self.p.text_dim, 15))
        self.draw_hint.setVisible(on)

    def _on_region_drawn(self, side: int, x0: float, y0: float,
                         x1: float, y1: float):
        page_no = None
        if self._current_stat:
            page_no = (self._current_stat.a_pno if side == 0
                       else self._current_stat.b_pno)
        rule = IgnoreRule(x0, y0, x1, y1, scope=SCOPE_ALL,
                          label=f"Region on page {(page_no or 0) + 1}")
        self._add_rule(rule)
        self.draw_btn.setChecked(False)

    def _add_rule(self, rule: IgnoreRule):
        self._ignore_rules.append(rule)
        self._refresh_rules_list()
        self.rules_list.setCurrentRow(len(self._ignore_rules) - 1)
        self.apply_btn.setEnabled(True)
        self._right_tabs.setCurrentIndex(1)
        if self._current_stat:
            self._update_ignore_overlay(self._current_stat)

    def _refresh_rules_list(self):
        self.rules_list.blockSignals(True)
        self.rules_list.clear()
        for rule in self._ignore_rules:
            item = QListWidgetItem(f"{rule.display_name()}\n{rule.scope_text()}")
            item.setCheckState(Qt.CheckState.Checked if rule.enabled
                               else Qt.CheckState.Unchecked)
            self.rules_list.addItem(item)
        self.rules_list.blockSignals(False)
        self.apply_btn.setEnabled(True)
        self._rule_editor.setEnabled(bool(self._ignore_rules))
        if not self._ignore_rules:
            self.apply_btn.setEnabled(False)

    def _on_rule_selected(self, row: int):
        if not (0 <= row < len(self._ignore_rules)):
            self._rule_editor.setEnabled(False)
            return
        rule = self._ignore_rules[row]
        self._rule_editor.setEnabled(True)
        for combo, value in ((self.scope_combo, rule.scope),
                             (self.side_combo, rule.side)):
            combo.blockSignals(True)
            idx = combo.findData(value)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)
        self.pages_edit.blockSignals(True)
        self.pages_edit.setText(rule.pages)
        self.pages_edit.blockSignals(False)
        self.pages_edit.setVisible(rule.scope == SCOPE_PAGES)

    def _update_selected_rule(self):
        row = self.rules_list.currentRow()
        if not (0 <= row < len(self._ignore_rules)):
            return
        rule = self._ignore_rules[row]
        rule.scope = self.scope_combo.currentData()
        rule.side = self.side_combo.currentData()
        rule.pages = self.pages_edit.text().strip()
        self.pages_edit.setVisible(rule.scope == SCOPE_PAGES)
        item = self.rules_list.item(row)
        if item:
            item.setText(f"{rule.display_name()}\n{rule.scope_text()}")
        if self._current_stat:
            self._update_ignore_overlay(self._current_stat)

    def _remove_rule(self):
        row = self.rules_list.currentRow()
        if 0 <= row < len(self._ignore_rules):
            self._ignore_rules.pop(row)
            self._refresh_rules_list()
            if self._current_stat:
                self._update_ignore_overlay(self._current_stat)

    def _clear_rules(self):
        self._ignore_rules.clear()
        self._refresh_rules_list()
        self.apply_btn.setEnabled(True)
        if self._current_stat:
            self._update_ignore_overlay(self._current_stat)

    def _update_ignore_overlay(self, stat: PageStat):
        iset = IgnoreSet(self._ignore_rules)
        rects_a = iset.rects_for(0, stat.a_pno) if stat.a_pno is not None else []
        rects_b = iset.rects_for(1, stat.b_pno) if stat.b_pno is not None else []
        self.viewer.set_ignore_rects(rects_a, rects_b)

    def _apply_rules(self):
        # Honour the per-row checkboxes before handing the list to the engine.
        for i, rule in enumerate(self._ignore_rules):
            item = self.rules_list.item(i)
            if item is not None:
                rule.enabled = item.checkState() == Qt.CheckState.Checked
        self.apply_btn.setEnabled(False)
        self.rediff_requested.emit([r.to_dict() for r in self._ignore_rules])

    def clear_ignore_rules(self):
        """Drop every exclusion rectangle, e.g. when a new run starts."""
        self._ignore_rules.clear()
        self._refresh_rules_list()
        if self._current_stat is not None:
            self._update_ignore_overlay(self._current_stat)

    def ignore_rules(self) -> list[dict]:
        return [r.to_dict() for r in self._ignore_rules]

    def set_busy(self, busy: bool):
        self.export_btn.setEnabled(not busy)
        self.apply_btn.setEnabled(not busy and bool(self._ignore_rules))
        self.settings_btn.setEnabled(not busy)
