"""Agent link UI: the "Agent suggestions" panel (review edit sets proposed by AI agents) and the Agents settings
page (turn the link on, token file, regenerate, recent activity). The protocol lives in leaf/agentlink.py."""
from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import agentlink as AL

ROLE = Qt.ItemDataRole.UserRole


class AgentPanel(QWidget):
    """Pending proposals for this window's document. Each proposal is a top-level row; its changes are
    checkable children. Accept applies the ticked changes of the current proposal as one undo step."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.link = AL.link()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.info = QLabel("Edits suggested by AI agents appear here. Nothing changes until you accept.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Agent suggestions")
        self.tree.itemDoubleClicked.connect(self._go_to)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setToolTip("Apply the ticked changes of the selected suggestion (one undo step)")
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setToolTip("Discard the selected suggestion")
        self.accept_all_btn = QPushButton("Accept all")
        self.accept_all_btn.setToolTip("Apply every change of every suggestion listed here")
        for b in (self.accept_btn, self.reject_btn, self.accept_all_btn):
            row.addWidget(b)
        lay.addLayout(row)
        self.auto = QCheckBox("Auto-accept further suggestions (this session)")
        self.auto.setToolTip("Apply new suggestions for this document right away, each as its own undo step,\n"
                             "until Leaf closes. Agents still can't save or sign.")
        self.auto.toggled.connect(self._auto_toggled)
        lay.addWidget(self.auto)
        self.accept_btn.clicked.connect(self.accept_current)
        self.reject_btn.clicked.connect(self.reject_current)
        self.accept_all_btn.clicked.connect(self.accept_all)
        self.link.proposalAdded.connect(self._added)
        self.link.proposalChanged.connect(self._changed)
        self._update_buttons()

    # ---------------------------------------------------------------- model
    def doc(self):
        return self.win.canvas.doc

    def proposal_items(self) -> list[QTreeWidgetItem]:
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _added(self, p) -> None:
        if p.doc is None or p.doc is not self.doc():
            return
        top = QTreeWidgetItem([f"{p.title}  —  {p.session}  ({len(p.changes)})"])
        top.setData(0, ROLE, p.id)
        top.setToolTip(0, f"From {p.session}. Tick the changes to keep, then Accept.")
        f = top.font(0)
        f.setBold(True)
        top.setFont(0, f)
        for ch in p.changes:
            it = QTreeWidgetItem([ch.summary])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked)
            it.setToolTip(0, json.dumps(ch.value, ensure_ascii=False, indent=1, default=str)[:2000])
            top.addChild(it)
        self.tree.addTopLevelItem(top)
        top.setExpanded(True)
        if self.tree.currentItem() is None:
            self.tree.setCurrentItem(top)
        self._update_buttons()
        dock = getattr(self.win, "agent_dock", None)
        if dock is not None and p.status == "pending" and not dock.isVisible():
            dock.show()
            dock.raise_()
        if p.status == "pending" and hasattr(self.win, "toast"):
            self.win.toast(f"{p.session} suggests: {p.title} — review it in Agent suggestions", ms=4000)

    def _changed(self, p) -> None:
        if p.status == "pending":
            return
        for top in self.proposal_items():
            if top.data(0, ROLE) == p.id:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(top))
        self._update_buttons()
        if p.status in ("accepted", "partially accepted") and p.doc is self.doc() and hasattr(self.win, "toast"):
            extra = f"  ·  {len(p.warnings)} warning(s)" if p.warnings else ""
            self.win.toast(f"Applied “{p.title}” ({p.accepted}/{len(p.changes)}){extra}. Undo with Ctrl+Z.", ms=4000)

    def _proposal(self, top: QTreeWidgetItem | None):
        if top is None:
            return None
        while top.parent() is not None:
            top = top.parent()
        return self.link.proposals.get(top.data(0, ROLE)), top

    def _current(self):
        cur = self.tree.currentItem() or (self.proposal_items()[0] if self.tree.topLevelItemCount() else None)
        r = self._proposal(cur)
        return r if r and r[0] is not None else (None, None)

    def _update_buttons(self) -> None:
        has = self.tree.topLevelItemCount() > 0
        for b in (self.accept_btn, self.reject_btn, self.accept_all_btn):
            b.setEnabled(has)
        self.auto.setEnabled(self.doc() is not None)

    # ---------------------------------------------------------------- actions
    def accept_current(self) -> list[str]:
        p, top = self._current()
        if p is None:
            return []
        chosen = [p.changes[i] for i in range(top.childCount()) if i < len(p.changes)
                  and top.child(i).checkState(0) == Qt.CheckState.Checked]
        return self.link.accept(p, chosen)

    def reject_current(self) -> None:
        p, _ = self._current()
        if p is not None:
            self.link.reject(p)

    def accept_all(self) -> None:
        for top in self.proposal_items():
            p = self.link.proposals.get(top.data(0, ROLE))
            if p is not None:
                self.link.accept(p)

    def _auto_toggled(self, on: bool) -> None:
        d = self.doc()
        if d is None:
            return
        key = AL.doc_id(d)
        if on:
            self.link.auto_accept.add(key)
            for p in self.link.pending_for(d):
                self.link.accept(p)
        else:
            self.link.auto_accept.discard(key)

    def _go_to(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        r = self._proposal(item)
        if not r or r[0] is None or item.parent() is None:
            return
        p, top = r
        ch = p.changes[top.indexOfChild(item)]
        if ch.page is not None and self.doc() is not None and 1 <= ch.page <= len(self.doc().pages):
            self.win.canvas.go_to(ch.page - 1)


# ------------------------------------------------------------------ settings page
def mcp_command() -> list[str]:
    """How to start Leaf's MCP server from this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    return [sys.executable, "-m", "leaf", "mcp"]


def mcp_config_json() -> str:
    cmd = mcp_command()
    return json.dumps({"mcpServers": {"leaf": {"command": cmd[0], "args": cmd[1:]}}}, indent=2)


class AgentSettingsPage(QWidget):
    """Settings › Agents. Changes apply immediately (no OK needed)."""

    def __init__(self, win=None):
        super().__init__()
        self.link = AL.link()
        lay = QVBoxLayout(self)
        self.enabled = QCheckBox("Let AI agents connect to Leaf (agent link)")
        self.enabled.setChecked(AL.is_enabled())
        self.enabled.toggled.connect(self._toggle)
        lay.addWidget(self.enabled)
        note = QLabel("Agents on this computer (for example through <code>leaf mcp</code>) can then list and read "
                      "your open documents and <b>suggest</b> edits; you review every suggestion in the Agent "
                      "suggestions panel. Agents can never save, sign or change anything without your click. "
                      "They authenticate with a secret token stored in a file only you can read.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(note)
        lay.addWidget(QLabel("Token file:"))
        self.path = QLabel(AL.token_path())
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setWordWrap(True)
        lay.addWidget(self.path)
        row = QHBoxLayout()
        self.regen = QPushButton("Regenerate token")
        self.regen.setToolTip("Revoke the current token (connected agents are disconnected) and create a new one")
        self.regen.clicked.connect(self._regen)
        self.copy = QPushButton("Copy MCP config")
        self.copy.setToolTip("Copy a JSON snippet that registers Leaf's MCP server (Claude Desktop, Claude Code, ...)")
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(mcp_config_json()))
        row.addWidget(self.regen)
        row.addWidget(self.copy)
        row.addStretch(1)
        lay.addLayout(row)
        self.state = QLabel()
        lay.addWidget(self.state)
        lay.addWidget(QLabel("Recent activity:"))
        self.log = QListWidget()
        self.log.setAccessibleName("Recent agent activity")
        lay.addWidget(self.log, 1)
        clear = QPushButton("Clear activity")
        clear.clicked.connect(AL.clear_log)
        lay.addWidget(clear, 0, Qt.AlignmentFlag.AlignLeft)
        self.link.logChanged.connect(self._refresh)
        self.link.sessionsChanged.connect(self._refresh)
        self._refresh()

    def _toggle(self, on: bool) -> None:
        self.link.set_enabled(on)
        self._refresh()

    def _regen(self) -> None:
        self.link.regenerate()
        self._refresh()

    def _refresh(self) -> None:
        on = AL.is_enabled()
        self.regen.setEnabled(on)
        n = len(self.link.sessions)
        if not on:
            self.state.setText("The agent link is off.")
        elif self.link.server is None:
            self.state.setText("On, but this Leaf isn't the running instance (another Leaf owns the link).")
        else:
            self.state.setText(f"On  ·  {n} agent{'s' * (n != 1)} connected" + ("" if os.path.exists(AL.token_path())
                                                                              else "  ·  token file missing"))
        self.log.clear()
        for e in AL.load_log():
            self.log.addItem(f"{e.get('time', '')}  {e.get('client', '')}: {e.get('event', '')}")


def _settings_dialog(win) -> None:
    d = QDialog(win)
    d.setWindowTitle("AI agents")
    lay = QVBoxLayout(d)
    lay.addWidget(AgentSettingsPage(win))
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(d.reject)
    lay.addWidget(bb)
    d.resize(560, 520)
    d.exec()


_registered = False


def _register_settings_page() -> bool:
    global _registered
    if _registered:
        return True
    try:
        from .. import settings as app_settings

        app_settings.register_page("agents", "Agents", lambda w: AgentSettingsPage(w), order=80,
                                   keywords="AI agent MCP token Claude link automation")
    except Exception:  # noqa: BLE001 - no settings dialog in this build: a menu entry opens the page instead
        return False
    _registered = True
    return True


# ------------------------------------------------------------------ install
def install(win) -> None:
    win.agent_panel = AgentPanel(win)
    dock = QDockWidget("Agent suggestions", win)
    dock.setObjectName("agent_suggestions")
    dock.setWidget(win.agent_panel)
    dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    dock.hide()
    win.agent_dock = dock
    a = dock.toggleViewAction()
    a.setText("&Agent suggestions panel")
    a.setObjectName("a_agent_panel")
    win.a_agent_panel = a
    menus = getattr(win, "menus", {})
    if "view" in menus:
        menus["view"].addAction(a)
    if not _register_settings_page():
        s = QAction("AI &agent link…", win)
        s.setObjectName("a_agent_settings")
        s.triggered.connect(lambda: _settings_dialog(win))
        win.a_agent_settings = s
        win.addAction(s)
        if "view" in menus:
            menus["view"].addAction(s)
