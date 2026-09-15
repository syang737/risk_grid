"""Delivering things to people.

Email first because everyone has it and no customer has to approve anything.
Slack is the obvious second, and the interface exists so adding it does not
touch the scheduler, the alert engine or the report renderer.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class Message:
    to: list[str]
    subject: str
    body: str
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


class Notifier(ABC):
    @abstractmethod
    def send(self, message: Message) -> None: ...


class ConsoleNotifier(Notifier):
    """Prints instead of sending. The default in development.

    Deliberately the default rather than a silently-dropping stub: a report that
    appears to send and does not is worse than one that obviously did not.
    """

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        self.sent.append(message)
        print(f"[notify] to={', '.join(message.to)} subject={message.subject!r} "
              f"attachments={[a.filename for a in message.attachments]}")


class SMTPNotifier(Notifier):
    """Real email. Works against SES, Postmark or any SMTP relay."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        sender: str = "risk_grid <no-reply@risk-grid.local>",
        use_tls: bool = True,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, message: Message) -> None:
        import smtplib
        from email.message import EmailMessage

        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = ", ".join(message.to)
        email["Subject"] = message.subject
        email.set_content(message.body)
        if message.html:
            email.add_alternative(message.html, subtype="html")

        for attachment in message.attachments:
            maintype, _, subtype = attachment.content_type.partition("/")
            email.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(email)


def default_notifier() -> Notifier:
    """Build a notifier from the environment.

    Falls back to the console rather than to a no-op, so a misconfigured
    deployment is noisy instead of quietly dropping alerts.
    """
    host = os.environ.get("RISK_GRID_SMTP_HOST")
    if not host:
        return ConsoleNotifier()
    return SMTPNotifier(
        host=host,
        port=int(os.environ.get("RISK_GRID_SMTP_PORT", 587)),
        username=os.environ.get("RISK_GRID_SMTP_USER"),
        password=os.environ.get("RISK_GRID_SMTP_PASSWORD"),
        sender=os.environ.get("RISK_GRID_SMTP_SENDER", "risk_grid <no-reply@risk-grid.local>"),
        use_tls=os.environ.get("RISK_GRID_SMTP_TLS", "1") != "0",
    )
