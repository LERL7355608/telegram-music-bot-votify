import inspect
import os

from providers.base import DownloadProvider


def build_provider(name: str) -> DownloadProvider:
    provider_name = name.strip().lower()

    if provider_name == "mock":
        from providers.mock import MockProvider

        return MockProvider()

    if provider_name == "custom":
        from providers.custom import CustomProvider

        if "token" in inspect.signature(CustomProvider).parameters:
            return CustomProvider(token=os.getenv("CUSTOM_PROVIDER_TOKEN"))
        return CustomProvider()

    if provider_name == "votify":
        from providers.votify import VotifyProvider

        return VotifyProvider()

    raise RuntimeError(f"Unknown provider: {provider_name}")
