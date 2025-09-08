# AWS Organizations MCP Server

A Model Context Protocol server providing AWS Organizations integration with support for institutions, projects, users, and budget monitoring.

## Demo

<video src="https://github.com/user-attachments/assets/e1101a25-82b5-4795-a9ee-bfe840289d3c" controls style="width: 48%; max-width: 600px;" alt="AWS Organizations MCP Server Demo">
  Your browser does not support the video tag.
</video>

## Features

- AWS Organizations, SSO, Identity Center, and Cost Explorer integration
- Multi-institution support
- User management and budget monitoring
- Docker support with HTTP transport mode

## Available Tools

- **`health_check`**: Check server and AWS connections
- **`get_institutions`**: List AWS accounts from Organizations
- **`get_projects`**: Get organizational units and sub-accounts
- **`get_users`**: Get users via AWS SSO/Identity Center
- **`get_tags`**: Get AWS resource tags
- **`check_budget`**: Budget monitoring with Cost Explorer
- **`verify_email`**: Verify user email (requires inia module)
- **`reset_password`**: Reset user password (requires inia module)

## Quick Setup

### 1. Create secrets.json

```json
{
  "institutions": {
    "sandbox": {
      "aws_access_key_id": "AKIA...",
      "aws_secret_access_key": "...",
      "description": "Sandbox environment"
    },
    "aueb": {
      "aws_access_key_id": "AKIA...",
      "aws_secret_access_key": "...",
      "description": "AUEB institution"
    },
    "grnet": {
      "aws_access_key_id": "AKIA...",
      "aws_secret_access_key": "...",
      "description": "GRNET institution"
    }
  }
}
```

### 2. Run with Docker

```bash
docker-compose up -d
```

### 3. Run Locally

```bash
pip install -r requirements.txt
python main.py --transport http --host 0.0.0.0 --port 8080
```

## MCP Configuration

### HTTP Mode

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "AWSMCPServer": {
      "type": "streamable-http",
      "url": "http://localhost:8080/mcp",
      "alwaysAllow": [
        "health_check",
        "get_institutions",
        "get_projects",
        "get_users",
        "get_tags",
        "check_budget"
      ]
    }
  }
}
```

## AWS Permissions Required

- `organizations:*`
- `sso:*` and `sso-admin:*`
- `identitystore:*`
- `ce:*`
- `tag:*`

## Usage Examples

- "Run a health check to verify AWS connections"
- "Get all institutions"
- "Get projects for institution 'grnet' with account ID '123456789012'"
- "Get users for 'sandbox' institution"
- "Check budget for 'aueb' institution this month"
