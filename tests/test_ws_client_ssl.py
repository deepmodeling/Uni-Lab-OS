from unittest.mock import patch

from unilabos.app.ssl_utils import create_wss_ssl_context


def test_wss_ssl_context_uses_certifi_ca_bundle() -> None:
    expected_context = object()

    with (
        patch("unilabos.app.ssl_utils.certifi.where", return_value="certifi-ca.pem"),
        patch(
            "unilabos.app.ssl_utils.ssl.create_default_context",
            return_value=expected_context,
        ) as create_default_context,
    ):
        actual_context = create_wss_ssl_context()

    assert actual_context is expected_context
    create_default_context.assert_called_once_with(cafile="certifi-ca.pem")
