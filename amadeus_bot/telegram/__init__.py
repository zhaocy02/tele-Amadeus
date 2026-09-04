"""Thin Telegram transport and routing layer."""

from .adapter import DownloadedImage, IncomingImageAttachment, IncomingMessage, TelegramGateway
from .autonomy_pilot import AutonomyPilotTickSummary, V2AutonomyPilotRunner
from .github_feedback_router import GitHubFeedbackCommandRouter
from .github_feedback_v2_router import V2_HELP_TEXT, V2TelegramMessageRouter
from .http_gateway import TelegramHTTPGateway, TelegramTransportError
from .poller import TelegramInboxStore, V2TelegramPollingRunner
from .provider_delivery import ProviderAwareV2TelegramDeliveryAdapter
from .router import HELP_TEXT, TelegramMessageRouter
from .updates import parse_authorized_message_update, parse_authorized_text_update
from .v2_delivery import (
    V2TelegramAutonomyDeliveryResult,
    V2TelegramDeliveryAdapter,
    V2TelegramUserDeliveryResult,
)

__all__ = [
    "AutonomyPilotTickSummary",
    "DownloadedImage",
    "GitHubFeedbackCommandRouter",
    "HELP_TEXT",
    "IncomingImageAttachment",
    "IncomingMessage",
    "ProviderAwareV2TelegramDeliveryAdapter",
    "TelegramGateway",
    "TelegramHTTPGateway",
    "TelegramInboxStore",
    "TelegramMessageRouter",
    "TelegramTransportError",
    "V2AutonomyPilotRunner",
    "V2_HELP_TEXT",
    "V2TelegramAutonomyDeliveryResult",
    "V2TelegramDeliveryAdapter",
    "V2TelegramMessageRouter",
    "V2TelegramPollingRunner",
    "V2TelegramUserDeliveryResult",
    "parse_authorized_message_update",
    "parse_authorized_text_update",
]
