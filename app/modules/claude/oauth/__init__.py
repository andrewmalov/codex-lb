"""Claude OAuth link flow.

authorization_code + PKCE + copy-paste code entry. Isolated from the
Codex OAuth flow at ``app.modules.oauth``; shares only the
:class:`app.core.crypto.TokenEncryptor` envelope.
"""
