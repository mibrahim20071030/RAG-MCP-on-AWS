# Project 3 Layer 7: deploy the MCP server (console steps)

All in **us-east-1**. `<api-id>` is the ID in the invoke URL of `datasheet-rag-api` (`https://<api-id>.execute-api.us-east-1.amazonaws.com`). `<account-id>` and the Aurora `cluster-...` resource ID are the same as in `runs/lambda_deploy.md`. Nothing here is a secret.

## 1. Cognito: a resource server for the deployed URL

Cognito → User pools → `datasheet-mcp`:

1. **Branding → Domain → Resource servers → Create resource server**:
   - Name: `datasheet-mcp-deployed`
   - Identifier: `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp` (exact, no trailing slash)
   - Custom scope: `search`
2. **Applications → App clients → `datasheet-mcp-client` → Login pages → Edit**. Under Custom scopes, tick `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp/search`. Save.

## 2. IAM role `datasheet-mcp-lambda`

IAM → Roles → Create role → AWS service, use case **Lambda** → attach **AWSLambdaBasicExecutionRole** → name `datasheet-mcp-lambda` → Create. Then Add permissions → Create inline policy → JSON, named `datasheet-mcp-runtime`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "Models", "Effect": "Allow", "Action": "bedrock:InvokeModel",
     "Resource": ["arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
                  "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0"]},
    {"Sid": "Rerank", "Effect": "Allow", "Action": "bedrock:Rerank", "Resource": "*"},
    {"Sid": "Aurora", "Effect": "Allow", "Action": "rds-db:connect",
     "Resource": "arn:aws:rds-db:us-east-1:<account-id>:dbuser:<cluster-resource-id>/rag_app"},
    {"Sid": "Limits", "Effect": "Allow", "Action": "dynamodb:UpdateItem",
     "Resource": "arn:aws:dynamodb:us-east-1:<account-id>:table/datasheet-rag-usage"}
  ]
}
```

On the dev user, add an inline policy `mcp-lambda-logs`, so Claude can read the function's log lines:

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "logs:FilterLogEvents",
  "Resource": "arn:aws:logs:us-east-1:<account-id>:log-group:/aws/lambda/datasheet-mcp:*"}]}
```

## 3. Lambda `datasheet-mcp`

Lambda → Create function → Author from scratch: name `datasheet-mcp`, runtime **Python 3.14**, **x86_64**, existing role `datasheet-mcp-lambda`. Then:

1. **Code** → Upload from → .zip file → `datasheet-rag\build\mcp.zip` → **Save**. Check that the code size is about 32.6 MB.
2. **Runtime settings** → Handler: `mcp_handler.lambda_handler`.
3. **General configuration**: Memory **512 MB**, Timeout **1 min 0 s**.
4. **Environment variables**:

| Key | Value |
|---|---|
| `DB_TARGET` | `aurora` |
| `AURORA_HOST` | as in `datasheet-rag` |
| `AURORA_DATABASE` | `postgres` |
| `AURORA_USER` | `rag_app` |
| `AURORA_CONNECT_ATTEMPTS` | `1` |
| `AURORA_CONNECT_TIMEOUT_S` | `19` |
| `RAG_RUNS_DIR` | `/tmp/runs` |
| `BEDROCK_MAX_ATTEMPTS` | `0` (botocore counts retries: 0 is one try) |
| `BEDROCK_CONNECT_TIMEOUT_S` | `1` |
| `BEDROCK_READ_TIMEOUT_S` | `2` |
| `RERANK_MIN_INTERVAL_S` | `0` |
| `USAGE_TABLE` | `datasheet-rag-usage` |
| `MCP_USER_PER_MINUTE` | `3` |
| `MCP_DAILY_LIMIT` | `100` |
| `MCP_RERANK_PER_MINUTE` | `3` |
| `MCP_COGNITO_ISSUER` | as in `datasheet-rag\.env` |
| `MCP_COGNITO_CLIENT_IDS` | as in `datasheet-rag\.env` |
| `MCP_PUBLIC_URL` | `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp` |
| `MCP_SCOPE` | `https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp/search` |

The connect and Bedrock values must fit the 25 s budget: connect 1 × 19 s, plus 2 Bedrock calls of 1 try × (1 + 2) s. If they do not fit, every call fails and the log names the settings. Write `MCP_PUBLIC_URL` in lower case, with no port.

## 4. API Gateway: routes and throttle

API Gateway → `datasheet-rag-api`:

1. **Develop → Integrations → Manage integrations → Create**: type Lambda, function `datasheet-mcp`, payload version **2.0**.
2. **Develop → Routes → Create**: `POST` `/mcp`, and `GET` `/.well-known/oauth-protected-resource/mcp`. Attach the `datasheet-mcp` integration to both. Add **no authorizer** (the server checks the token itself).
3. Lambda → `datasheet-mcp` → Configuration → Permissions → Resource-based policy statements. Check that there is a statement that lets `apigateway.amazonaws.com` invoke it. If there is none, the API answers 500 and the Lambda log stays empty.
4. **CloudShell** (one line each). First read the current route settings:

   ```bash
   aws apigatewayv2 get-stage --region us-east-1 --api-id <api-id> --stage-name '$default' --query RouteSettings
   ```

   Then set the two new routes. Copy every route the output showed, with its current values (do not use older values from the Project 2 notes), and add the two new ones:

   ```bash
   aws apigatewayv2 update-stage --region us-east-1 --api-id <api-id> --stage-name '$default' --route-settings '{<each route from the output, unchanged>,"POST /mcp":{"ThrottlingBurstLimit":5,"ThrottlingRateLimit":1},"GET /.well-known/oauth-protected-resource/mcp":{"ThrottlingBurstLimit":5,"ThrottlingRateLimit":1}}'
   ```

   A route key that does not exist (for example `GET /api/documents`, removed in Project 2 step 7b) makes AWS refuse the whole command with `NotFoundException`, and nothing is changed.

   Run `get-stage` again and check that all four are there.

## 5. DynamoDB

DynamoDB → Tables → `datasheet-rag-usage` → Additional settings → **Time to Live**. It should be on, with attribute `expires_at`. Project 2 turned it on; turn it on if it is off.

## 6. Hand over

Add `MCP_API_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com` to `datasheet-rag\.env`. Then tell Claude it is done, and give the Lambda code size shown in the console.
