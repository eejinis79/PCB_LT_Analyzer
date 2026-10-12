"""계정 화면: 비밀번호 잠금 · 초기 비밀번호 강제 변경 · KRX/DART 계정 편집."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout,
)

from ..vault import Credentials, Vault, VaultError, WrongPassword, validate_new_password
from .widgets import Card

MAX_FAILS = 5
LOCK_SECONDS = 30


def _busy(fn, *args):
    """키 유도(PBKDF2)가 0.5초가량 걸리므로 대기 커서 표시."""
    QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        return fn(*args)
    finally:
        QGuiApplication.restoreOverrideCursor()


def _pw_edit(placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.Password)
    e.setPlaceholderText(placeholder)
    return e


class _Base(QDialog):
    def __init__(self, title: str, heading: str, hint: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        self.card = Card(heading, hint)
        lay.addWidget(self.card)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #ff5c6c;")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        self.buttons_row = QHBoxLayout()
        lay.addLayout(self.buttons_row)

    def show_error(self, msg: str):
        self.error.setText(msg)
        self.error.setVisible(bool(msg))


class PasswordDialog(_Base):
    """계정 비밀번호 입력. 성공 시 self.password / self.creds 설정."""

    _fails = 0
    _locked_until = 0.0  # 클래스 변수: 창을 다시 열어도 잠금 유지

    def __init__(self, vault: Vault, parent=None):
        first = not vault.exists()
        super().__init__(
            "계정 잠금 해제", "🔒 계정 비밀번호",
            "계정 정보(KRX·DART)는 이 비밀번호로 암호화되어 저장됩니다."
            + ("\n처음 사용: 초기 비밀번호는 0000 입니다. 접속 후 반드시 변경해야 합니다." if first else ""),
            parent)
        self.vault = vault
        self.password = ""
        self.creds: Credentials | None = None
        self.pw = _pw_edit("비밀번호")
        self.card.body.addWidget(self.pw)

        reset = QPushButton("비밀번호를 잊었어요 (초기화)", objectName="Ghost")
        reset.clicked.connect(self._reset)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("확인")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._try)
        bb.rejected.connect(self.reject)
        self.buttons_row.addWidget(reset)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)
        self.pw.setFocus()

    def _try(self):
        wait = PasswordDialog._locked_until - time.time()
        if wait > 0:
            self.show_error(f"비밀번호를 {MAX_FAILS}회 틀려 {int(wait) + 1}초 동안 잠겼습니다.")
            return
        try:
            self.creds = _busy(self.vault.unlock, self.pw.text())
        except WrongPassword as e:
            PasswordDialog._fails += 1
            left = MAX_FAILS - PasswordDialog._fails
            if left <= 0:
                PasswordDialog._fails = 0
                PasswordDialog._locked_until = time.time() + LOCK_SECONDS
                self.show_error(f"비밀번호를 {MAX_FAILS}회 틀렸습니다. {LOCK_SECONDS}초 후 다시 시도하세요.")
            else:
                self.show_error(f"{e} (남은 시도 {left}회)")
            self.pw.selectAll()
            return
        except VaultError as e:
            self.show_error(str(e))
            return
        PasswordDialog._fails = 0
        self.password = self.pw.text()
        self.accept()

    def _reset(self):
        ok = QMessageBox.warning(
            self, "계정 초기화",
            "저장된 KRX·DART 계정 정보가 모두 삭제되고 비밀번호가 0000 으로 초기화됩니다.\n계속할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok == QMessageBox.Yes:
            self.vault.reset()
            self.show_error("초기화되었습니다. 0000 으로 접속한 뒤 새 비밀번호를 설정하세요.")


class ChangePasswordDialog(_Base):
    """비밀번호 변경. forced=True 면 초기 비밀번호 사용 중이라 변경해야만 진행 가능."""

    def __init__(self, vault: Vault, current: str | None = None, forced: bool = False, parent=None):
        super().__init__(
            "비밀번호 변경", "초기 비밀번호 변경 필요" if forced else "비밀번호 변경",
            ("초기 비밀번호(0000)로 접속했습니다. 계속하려면 새 비밀번호를 설정하세요.\n" if forced else "")
            + "4자 이상, 0000 사용 불가. 비밀번호를 잊으면 저장된 계정 정보는 복구할 수 없습니다.",
            parent)
        self.vault = vault
        self.new_password = ""
        self.creds: Credentials | None = None
        form = QFormLayout()
        form.setSpacing(10)
        self.cur = _pw_edit()
        if current is not None:
            self.cur.setText(current)
        else:
            form.addRow("현재 비밀번호", self.cur)
        self.new = _pw_edit()
        self.confirm = _pw_edit()
        form.addRow("새 비밀번호", self.new)
        form.addRow("새 비밀번호 확인", self.confirm)
        self.card.body.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("변경")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)
        (self.new if current is not None else self.cur).setFocus()

    def _apply(self):
        err = validate_new_password(self.new.text(), self.confirm.text())
        if err:
            self.show_error(err)
            return
        try:
            self.creds = _busy(self.vault.change_password, self.cur.text(), self.new.text(),
                               self.confirm.text())
        except VaultError as e:
            self.show_error(str(e))
            return
        self.new_password = self.new.text()
        self.accept()


def unlock(vault: Vault, parent=None) -> tuple[str, Credentials] | None:
    """비밀번호 확인(+ 초기 비밀번호면 강제 변경). 취소/실패 시 None."""
    dlg = PasswordDialog(vault, parent)
    if dlg.exec() != QDialog.Accepted:
        return None
    password, creds = dlg.password, dlg.creds
    if vault.must_change_password:
        ch = ChangePasswordDialog(vault, current=password, forced=True, parent=parent)
        if ch.exec() != QDialog.Accepted:
            QMessageBox.information(parent, "계정", "비밀번호를 변경해야 계정을 사용할 수 있습니다.")
            return None
        password, creds = ch.new_password, ch.creds
        QMessageBox.information(parent, "계정", "비밀번호가 변경되었습니다.")
    return password, creds


class AccountDialog(_Base):
    """KRX / DART 계정 정보 편집 (잠금 해제 후에만 열림)."""

    def __init__(self, vault: Vault, password: str, creds: Credentials, parent=None):
        super().__init__("계정 설정", "KRX 정보데이터시스템",
                         "data.krx.co.kr 로그인 계정 — 시세·투자자별 순매수 수집에 사용", parent)
        self.vault, self.password = vault, password
        self.setMinimumWidth(520)

        krx_form = QFormLayout()
        krx_form.setSpacing(10)
        self.krx_id = QLineEdit(creds.krx_id)
        self.krx_pw = _pw_edit()
        self.krx_pw.setText(creds.krx_pw)
        krx_form.addRow("KRX ID", self.krx_id)
        krx_form.addRow("KRX 비밀번호", self.krx_pw)
        self.card.body.addLayout(krx_form)

        dart = Card("OpenDART", "API 호출에는 '인증키'만 사용됩니다. "
                                "opendart.fss.or.kr 로그인 → 인증키 신청/관리에서 40자리 키를 발급받으세요.")
        dart_form = QFormLayout()
        dart_form.setSpacing(10)
        self.dart_id = QLineEdit(creds.dart_id)
        self.dart_pw = _pw_edit()
        self.dart_pw.setText(creds.dart_pw)
        self.dart_key = _pw_edit("40자리 인증키")
        self.dart_key.setText(creds.dart_key)
        dart_form.addRow("DART ID", self.dart_id)
        dart_form.addRow("DART 비밀번호", self.dart_pw)
        dart_form.addRow("API 인증키", self.dart_key)
        dart.body.addLayout(dart_form)
        self.layout().insertWidget(1, dart)

        tg = Card("텔레그램 알림 (선택)", "자동 실행(daily_run) 결과를 받을 봇. 텔레그램 @BotFather → /newbot 으로 토큰 발급 → "
                                       "만든 봇에게 아무 말이나 보낸 뒤 '채팅 ID 찾기'.")
        tg_form = QFormLayout()
        tg_form.setSpacing(10)
        self.tg_token = _pw_edit("123456:ABC-...")
        self.tg_token.setText(creds.telegram_token)
        self.tg_chat = QLineEdit(creds.telegram_chat)
        find = QPushButton("채팅 ID 찾기", objectName="Ghost")
        find.clicked.connect(self._find_chat)
        test = QPushButton("테스트 전송", objectName="Ghost")
        test.clicked.connect(self._test_send)
        tg_form.addRow("봇 토큰", self.tg_token)
        tg_form.addRow("채팅 ID", self.tg_chat)
        tg.body.addLayout(tg_form)
        row = QHBoxLayout()
        row.addWidget(find)
        row.addWidget(test)
        row.addStretch(1)
        tg.body.addLayout(row)
        self.layout().insertWidget(2, tg)

        self.show_btn = QPushButton("비밀번호 보기", objectName="Ghost", checkable=True)
        self.show_btn.toggled.connect(self._toggle_echo)
        change = QPushButton("계정 비밀번호 변경")
        change.clicked.connect(self._change_password)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("저장")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        self.buttons_row.addWidget(change)
        self.buttons_row.addWidget(self.show_btn)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)

    def _toggle_echo(self, on: bool):
        mode = QLineEdit.Normal if on else QLineEdit.Password
        for e in (self.krx_pw, self.dart_pw, self.dart_key, self.tg_token):
            e.setEchoMode(mode)
        self.show_btn.setText("비밀번호 숨기기" if on else "비밀번호 보기")

    def creds(self) -> Credentials:
        return Credentials(self.krx_id.text().strip(), self.krx_pw.text(), self.dart_id.text().strip(),
                           self.dart_pw.text(), self.dart_key.text().strip(),
                           self.tg_token.text().strip(), self.tg_chat.text().strip())

    def _find_chat(self):
        from ..notify import NotifyError, find_chat_id
        try:
            cid = _busy(find_chat_id, self.tg_token.text().strip())
        except (NotifyError, Exception) as e:  # noqa: BLE001
            self.show_error(str(e))
            return
        if cid:
            self.tg_chat.setText(cid)
            self.show_error("")
        else:
            self.show_error("봇에게 먼저 아무 메시지나 보낸 뒤 다시 눌러 주세요.")

    def _test_send(self):
        from ..notify import send_telegram
        try:
            _busy(send_telegram, self.tg_token.text().strip(), self.tg_chat.text().strip(),
                  "✅ Volume Spike Screener 테스트 메시지")
        except Exception as e:  # noqa: BLE001
            self.show_error(str(e))
            return
        QMessageBox.information(self, "텔레그램", "전송했습니다. 텔레그램을 확인하세요.")

    def _change_password(self):
        dlg = ChangePasswordDialog(self.vault, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.password = dlg.new_password
            # 화면에서 수정 중이던 내용도 새 비밀번호로 함께 저장
            _busy(self.vault.save, self.password, self.creds())
            QMessageBox.information(self, "계정", "계정 비밀번호가 변경되었습니다.")

    def _save(self):
        key = self.dart_key.text().strip()
        if key and len(key) != 40:
            if QMessageBox.question(self, "DART 인증키", f"인증키 길이가 {len(key)}자입니다 (보통 40자). 그대로 저장할까요?") \
                    != QMessageBox.Yes:
                return
        try:
            _busy(self.vault.save, self.password, self.creds())
        except OSError as e:
            self.show_error(f"저장 실패: {e}")
            return
        self.accept()
