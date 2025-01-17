from typing import Dict, List, Optional

import lxml
import toronado
from django.conf import settings
from django.core import exceptions, mail
from django.db import transaction
from django.template.loader import get_template
from django.utils import timezone
from sentry_sdk import capture_exception

from posthog.celery import app
from posthog.models.messaging import MessagingRecord


def inline_css(value: str) -> str:
    """
    Returns an HTML document with inline CSS.
    Forked from getsentry/sentry
    """
    tree = lxml.html.document_fromstring(value)
    toronado.inline(tree)
    # CSS media query support is inconsistent when the DOCTYPE declaration is
    # missing, so we force it to HTML5 here.
    return lxml.html.tostring(tree, doctype="<!DOCTYPE html>").decode("utf-8")


def is_email_available() -> bool:
    """
    Returns whether email services are available on this instance (i.e. settings are in place).
    """
    return bool(settings.EMAIL_HOST)


@app.task(ignore_result=True, max_retries=3)
def _send_email(
    campaign_key: str, to: List[Dict[str, str]], subject: str, headers: Dict, txt_body: str = "", html_body: str = "",
) -> None:
    """
    Sends built email message asynchronously.
    """

    messages: List = []
    records: List = []

    with transaction.atomic():

        for dest in to:
            record, _ = MessagingRecord.objects.get_or_create(raw_email=dest["raw_email"], campaign_key=campaign_key)

            # Lock object (database-level) while the message is sent
            record = MessagingRecord.objects.select_for_update().get(pk=record.pk)
            # If an email for this campaign was already sent to this user, skip recipient
            if record.sent_at:
                record.save()  # release DB lock
                continue

            records.append(record)

            email_message = mail.EmailMultiAlternatives(
                subject=subject, body=txt_body, to=[dest["recipient"]], headers=headers,
            )

            email_message.attach_alternative(html_body, "text/html")
            messages.append(email_message)

        connection = None
        try:
            connection = mail.get_connection()
            connection.open()
            connection.send_messages(messages)

            for record in records:
                record.sent_at = timezone.now()
                record.save()

        except Exception as e:
            # Handle exceptions gracefully to avoid breaking the entire task for all teams
            # but make sure they're tracked on Sentry.
            capture_exception(e)
        finally:
            # ensure that connection has been closed
            try:
                connection.close()  # type: ignore
            except Exception:
                pass


class EmailMessage:
    def __init__(
        self, campaign_key: str, subject: str, template_name: str, template_context: Optional[Dict] = None,
    ):
        if not is_email_available():
            raise exceptions.ImproperlyConfigured(
                "Email settings not configured! Set at least the EMAIL_HOST environment variable.",
            )

        self.campaign_key = campaign_key
        self.subject = subject
        template = get_template(f"email/{template_name}.html")
        self.html_body = inline_css(template.render(template_context))
        self.txt_body = ""
        self.headers: Dict = {}
        self.to: List[Dict[str, str]] = []

    def add_recipient(self, email: str, name: Optional[str] = None) -> None:
        self.to.append({"recipient": f'"{name}" <{email}>' if name else email, "raw_email": email})

    def send(self, send_async: bool = True) -> None:

        if not self.to:
            raise ValueError("No recipients provided! Use EmailMessage.add_recipient() first!")

        kwargs = {
            "campaign_key": self.campaign_key,
            "to": self.to,
            "subject": self.subject,
            "headers": self.headers,
            "txt_body": self.txt_body,
            "html_body": self.html_body,
        }

        if send_async:
            _send_email.apply_async(kwargs=kwargs)
        else:
            _send_email.apply(kwargs=kwargs)
