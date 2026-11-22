# Project 3 Layer 5: Cognito setup for the MCP login

A new pool, separate from Project 2's `datasheet-rag-review`. Region **us-east-1**. Cost: $0 (free tier).

1. **Cognito → User pools → Create user pool.**
   - Application type: **Single-page application (SPA)** (a public client with no secret). App name: `datasheet-mcp-client`.
   - Sign-in: **Email**. Self-registration **off**.
   - Return URL: `http://localhost:8765/callback`. Create.
   - Rename the pool to `datasheet-mcp` if the console named it something else (Overview → Rename).
   - Settings → Feature plans: keep **Essentials**. Never **Plus**.
2. **Resource server** (Branding → Domain → Resource servers → Create):
   - Name `datasheets`, identifier `datasheets`.
   - Custom scope: name `search`, description `Search the datasheets`.
3. **App client** (App clients → `datasheet-mcp-client` → Login pages → Edit):
   - Allowed callback URLs: `http://localhost:8765/callback` and `https://claude.ai/api/mcp/auth_callback`.
   - OAuth grant types: **Authorization code grant** only.
   - OpenID Connect scopes: `openid`. Custom scopes: `datasheets/search`.
   - Check that the client has **no client secret**.
4. **User:** Users → Create user with your email and a password you set.
5. **Copy these into `datasheet-rag\.env`** (never into a chat or a committed file):
   ```
   MCP_COGNITO_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/<User pool ID>
   MCP_COGNITO_CLIENT_IDS=<Client ID>
   MCP_COGNITO_DOMAIN=https://<prefix>.auth.us-east-1.amazoncognito.com
   ```
   The domain is under Branding → Domain. Then tell Claude.

If a screen looks different, or a step is refused, describe it or copy the message to Claude.

## Results (2026-11-21)

- Console work done by the human. The domain prefix is in `.env` (`MCP_COGNITO_DOMAIN`).
- Real login (`scripts/mcp_login.py`): no token 401 with `resource_metadata`; metadata 200; search with the token 200.
  The token has `token_use=access`, `scope=datasheets/search openid` and no `aud`.
- With `--resource http://localhost:8000/mcp`, Cognito refused the login (`invalid_target`): a bound token's custom
  scope must belong to a resource server whose identifier is that URL.
- Added by the human: a second resource server `mcp-local`, identifier `http://localhost:8000/mcp`, scope `search`,
  ticked on the app client. With `MCP_SCOPE=http://localhost:8000/mcp/search` and `--resource`, the token had
  `aud = http://localhost:8000/mcp` and the search returned 200.
