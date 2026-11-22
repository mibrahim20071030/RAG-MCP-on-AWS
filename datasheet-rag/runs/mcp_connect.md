# Project 3 Layer 8: connect Claude to the deployed server (console steps)

`<api-id>` is `m6xvt7cua7`. The client ID is `MCP_COGNITO_CLIENT_IDS` in `datasheet-rag\.env`. Nothing here is a secret.

## 1. Upload the new zip (log line with the `initialize` fields)

1. Claude builds `datasheet-rag\build\mcp.zip` (`.venv\Scripts\python.exe scripts\build_lambda.py`).
2. Lambda → `datasheet-mcp` → Code → Upload from → .zip file → `build\mcp.zip` → Save. The code size should be about 32.6 MB.

## 2. Check the Cognito app client

Cognito → User pools → `datasheet-mcp` → Applications → App clients → `datasheet-mcp-client` → Login pages:

- Allowed callback URLs include `https://claude.ai/api/mcp/auth_callback`.
- Custom scopes include `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp/search`.
- There is no client secret (it is a public client).

## 3. Add the connector in claude.ai

claude.ai → Customize → Connectors → **Add custom connector**:

- Name: `Datasheets`
- URL: `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp`
- Advanced settings → OAuth client: **Use your own OAuth client**. Client ID: the value from `.env`. Leave the secret **blank**.

Why: Cognito offers neither of Claude's other two ways. Its discovery document does not list `client_id_metadata_document_supported` or `none` (needed for "Claude's published identity"), and dynamic client registration is turned off (it would let anyone register a client).

Click **Connect**. The Cognito login page opens. Sign in with the test user. You can change the authentication settings only by removing the connector and adding it again.

## 4. One question

Start a new chat, turn on the `Datasheets` connector, and ask:

> Using the datasheets connector, what is the absolute maximum supply voltage of the LM35? Cite the page.

Then tell Claude the time you asked, what Claude answered, and any error text you saw. Claude reads the Lambda log from there.
