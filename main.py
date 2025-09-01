#!/usr/bin/env python3
"""
Real AWS Organizations Integration MCP Server using FastMCP 2.0+ with decorators
Provides AWS-integrated tools for managing institutions, projects, and users using real AWS APIs
Now supports multi-institution credential management
"""

import sys
import os
import json
import logging
import boto3
from datetime import datetime, timezone, timedelta, date
from typing import Dict, Any, List, Optional, Union
from functools import lru_cache
import time
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

from fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import inia module for SSO operations
try:
    from inia.singlesignon.client import SingleSignOnClient
    INIA_AVAILABLE = True
    logger.info("inia module loaded successfully for email verification and password reset")
except ImportError:
    INIA_AVAILABLE = False
    logger.warning("inia module not available. Email verification and password reset tools will be disabled.")

# Initialize FastMCP server
mcp = FastMCP("Real AWS Organizations MCP Server")

# Global variables for AWS client management
institutions_credentials: Optional[Dict[str, Dict[str, str]]] = None
aws_clients_cache: Dict[str, Any] = {}

def load_secrets_file() -> Dict[str, Any]:
    """Load and parse the multi-institution secrets.json file with AWS credentials."""
    secrets_path = os.path.join(os.path.dirname(__file__), "secrets.json")
    
    try:
        with open(secrets_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"Successfully loaded multi-institution AWS credentials from {secrets_path}")
            return data
    except FileNotFoundError:
        logger.error(f"Secrets file not found at {secrets_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in secrets file: {e}")
        raise
    except Exception as e:
        logger.error(f"Error loading secrets file: {e}")
        raise

def initialize_aws_credentials():
    """Initialize AWS credentials from multi-institution secrets file."""
    global institutions_credentials
    
    try:
        secrets_data = load_secrets_file()
        institutions_credentials = secrets_data.get('institutions', {})
        
        if not institutions_credentials:
            raise ValueError("No institutions found in secrets.json")
        
        # Validate that each institution has required credentials
        for institution_name, credentials in institutions_credentials.items():
            if not credentials.get('aws_access_key_id') or not credentials.get('aws_secret_access_key'):
                raise ValueError(f"Missing required AWS credentials for institution '{institution_name}'")
        
        logger.info(f"AWS credentials initialized successfully for {len(institutions_credentials)} institutions: {list(institutions_credentials.keys())}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize AWS credentials: {e}")
        raise

def get_available_institutions() -> List[str]:
    """Get list of available institution names from loaded credentials."""
    if not institutions_credentials:
        return []
    return list(institutions_credentials.keys())

def get_institution_credentials(institution_name: str) -> Optional[Dict[str, str]]:
    """Get AWS credentials for a specific institution."""
    if not institutions_credentials:
        logger.error("Institution credentials not initialized")
        return None
    
    if institution_name not in institutions_credentials:
        logger.error(f"Institution '{institution_name}' not found. Available institutions: {list(institutions_credentials.keys())}")
        return None
    
    return institutions_credentials[institution_name]

def validate_institution(institution_name: str) -> bool:
    """Validate that an institution name exists in the credentials."""
    return institution_name in (institutions_credentials or {})

def get_aws_client(service: str, institution: str, region: str = 'us-east-1') -> Optional[Any]:
    """Create and cache AWS clients for different services using institution-specific credentials."""
    cache_key = f"{service}_{institution}_{region}"
    
    if cache_key in aws_clients_cache:
        return aws_clients_cache[cache_key]
    
    try:
        # Get institution-specific credentials
        credentials = get_institution_credentials(institution)
        if not credentials:
            logger.error(f"Could not get credentials for institution '{institution}'")
            return None
            
        # Create boto3 client with institution-specific credentials
        client = boto3.client(
            service,
            aws_access_key_id=credentials['aws_access_key_id'],
            aws_secret_access_key=credentials['aws_secret_access_key'],
            region_name=region
        )
        
        # Cache the client
        aws_clients_cache[cache_key] = client
        logger.info(f"Created AWS {service} client for institution '{institution}' in region {region}")
        
        return client
        
    except Exception as e:
        logger.error(f"Failed to create AWS client for {service}/{institution}/{region}: {e}")
        return None

def sanitize_aws_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove sensitive AWS credentials from responses."""
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            if key.lower() in ['aws_access_key_id', 'aws_secret_access_key', 'secret_access_key', 'access_key']:
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, dict):
                sanitized[key] = sanitize_aws_response(value)
            elif isinstance(value, list):
                sanitized[key] = [sanitize_aws_response(item) if isinstance(item, dict) else item for item in value]
            else:
                sanitized[key] = value
        return sanitized
    return data

def handle_aws_error(error: Exception, operation: str, institution: Optional[str] = None) -> Dict[str, Any]:
    """Handle AWS API errors and return standardized error response."""
    error_response = {
        "success": False,
        "operation": operation
    }
    
    if institution:
        error_response["institution"] = institution
    
    if isinstance(error, NoCredentialsError):
        error_response.update({
            "error": "AWS credentials not found or invalid",
            "error_type": "credentials_error"
        })
    elif isinstance(error, PartialCredentialsError):
        error_response.update({
            "error": "Incomplete AWS credentials provided",
            "error_type": "credentials_error"
        })
    elif isinstance(error, ClientError):
        error_code = error.response['Error']['Code']
        error_message = error.response['Error']['Message']
        error_response.update({
            "error": f"AWS API Error: {error_message}",
            "error_code": error_code,
            "error_type": "aws_api_error"
        })
    else:
        error_response.update({
            "error": str(error),
            "error_type": "general_error"
        })
    
    return error_response

# Register institutions as MCP resources
def register_institution_resources():
    """Register institutions as MCP resources with URI pattern."""
    try:
        @mcp.resource("institution://institutions/{institution_id}")
        def get_institution_resource(institution_id: str):
            """Get institution resource data from AWS Organizations."""
            try:
                # For resource access, we need to determine which institution to use
                # We'll try each available institution until we find the account
                available_institutions = get_available_institutions()
                
                for institution_name in available_institutions:
                    try:
                        orgs_client = get_aws_client('organizations', institution_name)
                        if not orgs_client:
                            continue
                        
                        # Get account details
                        account = orgs_client.describe_account(AccountId=institution_id)
                        account_info = account['Account']
                        
                        # Get account tags
                        try:
                            tags_response = orgs_client.list_tags_for_resource(ResourceId=institution_id)
                            tags = {tag['Key']: tag['Value'] for tag in tags_response.get('Tags', [])}
                        except ClientError:
                            tags = {}
                        
                        return {
                            "uri": f"institution://institutions/{institution_id}",
                            "name": f"Institution: {account_info.get('Name', institution_id)}",
                            "description": tags.get('Description', f"AWS Account {institution_id}"),
                            "mimeType": "application/json",
                            "metadata": {
                                "account_id": account_info['Id'],
                                "name": account_info['Name'],
                                "email": account_info['Email'],
                                "status": account_info['Status'],
                                "joined_method": account_info['JoinedMethod'],
                                "joined_timestamp": account_info['JoinedTimestamp'].isoformat() if account_info.get('JoinedTimestamp') else None,
                                "tags": tags,
                                "accessed_via_institution": institution_name
                            }
                        }
                        
                    except ClientError:
                        # Try next institution
                        continue
                
                # If we get here, account wasn't found in any institution
                raise ValueError(f"Institution {institution_id} not found in any configured AWS organization")
                
            except Exception as e:
                logger.error(f"Error getting institution resource {institution_id}: {e}")
                raise ValueError(f"Institution {institution_id} not found or inaccessible")
                
        logger.info("Registered institution resource template")
        
    except Exception as e:
        logger.error(f"Failed to register institution resources: {e}")

@mcp.tool()
def health_check() -> Dict[str, Any]:
    """Perform a basic health check of the MCP server and AWS connection."""
    try:
        # Check AWS credentials
        credentials_loaded = institutions_credentials is not None
        available_institutions = get_available_institutions()
        
        # Test AWS connections for each institution
        institution_status = {}
        
        if credentials_loaded:
            for institution_name in available_institutions:
                try:
                    orgs_client = get_aws_client('organizations', institution_name)
                    if orgs_client:
                        # Try to describe the organization
                        orgs_client.describe_organization()
                        institution_status[institution_name] = "connected"
                except Exception as e:
                    institution_status[institution_name] = f"error: {str(e)}"
        
        return {
            "success": True,
            "health": {
                "server": "healthy",
                "status": "running",
                "multi_institution_support": True,
                "credentials_loaded": credentials_loaded,
                "available_institutions": available_institutions,
                "institution_count": len(available_institutions),
                "institution_status": institution_status,
                "aws_clients_cached": len(aws_clients_cache)
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Error in health_check: {e}")
        return {
            "success": False,
            "error": str(e)
        }

# Updated AWS Organizations Integration Tools
@mcp.tool()
def get_institutions(institution: Optional[str] = None, institution_type: Optional[str] = None, include_details: bool = False) -> Dict[str, Any]:
    """
    Get filtered list of institutions (AWS accounts) from AWS Organizations or list available institutions.
    
    Args:
        institution: Optional institution name to query specific AWS organization (sandbox, aueb, grnet)
        institution_type: Filter by type from account tags (academic, research, commercial)
        include_details: Include full account details and tags
    """
    try:
        # If no institution specified, return list of available institutions from secrets.json
        if not institution:
            available_institutions = get_available_institutions()
            return {
                "success": True,
                "data": {
                    "available_institutions": available_institutions,
                    "institution_count": len(available_institutions),
                    "description": "Available institutions from configuration"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Getting institutions from AWS Organizations for '{institution}' - type: {institution_type}, details: {include_details}")
        
        orgs_client = get_aws_client('organizations', institution)
        if not orgs_client:
            return {
                "success": False,
                "error": f"Could not create AWS Organizations client for institution '{institution}'"
            }
        
        # Get all accounts in the organization
        accounts_response = orgs_client.list_accounts()
        accounts = accounts_response['Accounts']
        
        institutions = []
        
        for account in accounts:
            account_id = account['Id']
            
            # Get account tags for filtering and metadata
            try:
                tags_response = orgs_client.list_tags_for_resource(ResourceId=account_id)
                tags = {tag['Key']: tag['Value'] for tag in tags_response.get('Tags', [])}
            except ClientError as e:
                logger.warning(f"Could not get tags for account {account_id}: {e}")
                tags = {}
            
            # Apply type filter if specified
            account_type = tags.get('Type', tags.get('InstitutionType', 'unknown'))
            if institution_type and account_type.lower() != institution_type.lower():
                continue
            
            if include_details:
                # Return full details
                inst_data = {
                    'id': account['Id'],
                    'name': account['Name'],
                    'email': account['Email'],
                    'status': account['Status'],
                    'joined_method': account['JoinedMethod'],
                    'joined_timestamp': account['JoinedTimestamp'].isoformat() if account.get('JoinedTimestamp') else None,
                    'type': account_type,
                    'description': tags.get('Description', f"AWS Account {account['Name']}"),
                    'budget': tags.get('Budget', 'Not specified'),
                    'tags': tags
                }
            else:
                # Return basic info only
                inst_data = {
                    'id': account['Id'],
                    'name': account['Name'],
                    'type': account_type,
                    'description': tags.get('Description', f"AWS Account {account['Name']}"),
                    'status': account['Status']
                }
            
            institutions.append(inst_data)
        
        return {
            "success": True,
            "data": {
                "institution": institution,
                "institutions": institutions,
                "count": len(institutions),
                "total_accounts": len(accounts),
                "filter_applied": institution_type,
                "details_included": include_details
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in get_institutions: {e}")
        return handle_aws_error(e, "get_institutions", institution)

@mcp.tool()
def get_projects(institution: str, institution_id: str, include_aws_details: bool = False) -> Dict[str, Any]:
    """
    Get projects (organizational units and sub-accounts) for a specific institution.
    
    Args:
        institution: Required institution name (sandbox, aueb, grnet)
        institution_id: Required AWS account ID
        include_aws_details: Include AWS organizational unit details
    """
    try:
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Getting comprehensive projects for institution '{institution}' account: {institution_id}, AWS details: {include_aws_details}")
        
        orgs_client = get_aws_client('organizations', institution)
        if not orgs_client:
            return {
                "success": False,
                "error": f"Could not create AWS Organizations client for institution '{institution}'"
            }
        
        # Get organization information and root
        try:
            org_response = orgs_client.describe_organization()
            organization = org_response['Organization']
            
            # Get organization roots
            roots_response = orgs_client.list_roots()
            if not roots_response.get('Roots'):
                return {
                    "success": False,
                    "error": "No organization roots found"
                }
            
            root_id = roots_response['Roots'][0]['Id']
            logger.info(f"Found organization root: {root_id}")
            
        except ClientError as e:
            return {
                "success": False,
                "error": f"Could not access organization: {e.response['Error']['Message']}"
            }
        
        # Get all accounts in the organization
        all_accounts = []
        try:
            paginator = orgs_client.get_paginator('list_accounts')
            for page in paginator.paginate():
                all_accounts.extend(page['Accounts'])
            
            logger.info(f"Found {len(all_accounts)} total accounts in organization")
            
        except ClientError as e:
            logger.error(f"Could not list accounts: {e}")
            return {
                "success": False,
                "error": f"Could not list organization accounts: {e.response['Error']['Message']}"
            }
        
        # Verify the requested account exists
        target_account = None
        for account in all_accounts:
            if account['Id'] == institution_id:
                target_account = account
                break
        
        if not target_account:
            return {
                "success": False,
                "error": f"Account '{institution_id}' not found in institution '{institution}'. Available accounts: {[acc['Id'] for acc in all_accounts[:5]]}"
            }
        
        # Get all organizational units recursively
        def get_all_ous(parent_id, level=0):
            """Recursively get all organizational units."""
            ous = []
            try:
                paginator = orgs_client.get_paginator('list_organizational_units_for_parent')
                for page in paginator.paginate(ParentId=parent_id):
                    for ou in page['OrganizationalUnits']:
                        ou['Level'] = level
                        ou['ParentId'] = parent_id
                        ous.append(ou)
                        # Recursively get child OUs
                        child_ous = get_all_ous(ou['Id'], level + 1)
                        ous.extend(child_ous)
            except ClientError as e:
                logger.warning(f"Could not list OUs for parent {parent_id}: {e}")
            
            return ous
        
        # Get all organizational units starting from root
        all_ous = get_all_ous(root_id)
        logger.info(f"Found {len(all_ous)} organizational units")
        
        # Build comprehensive projects list
        projects = []
        
        # Add all accounts as potential projects
        for account in all_accounts:
            account_id = account['Id']
            
            # Get account tags
            try:
                tags_response = orgs_client.list_tags_for_resource(ResourceId=account_id)
                account_tags = {tag['Key']: tag['Value'] for tag in tags_response.get('Tags', [])}
            except ClientError:
                account_tags = {}
            
            # Find which OU this account belongs to
            account_parent = None
            account_ou_path = []
            
            try:
                parents_response = orgs_client.list_parents(ChildId=account_id)
                if parents_response.get('Parents'):
                    parent = parents_response['Parents'][0]
                    account_parent = parent['Id']
                    
                    # Build OU path for this account
                    if parent['Type'] == 'ORGANIZATIONAL_UNIT':
                        for ou in all_ous:
                            if ou['Id'] == parent['Id']:
                                account_ou_path = [ou['Name']]
                                # Find parent path
                                current_parent = ou.get('ParentId')
                                while current_parent and current_parent != root_id:
                                    for parent_ou in all_ous:
                                        if parent_ou['Id'] == current_parent:
                                            account_ou_path.insert(0, parent_ou['Name'])
                                            current_parent = parent_ou.get('ParentId')
                                            break
                                    else:
                                        break
                                break
                        
            except ClientError:
                account_parent = root_id
            
            project_data = {
                'id': account_id,
                'name': account['Name'],
                'type': 'aws_account',
                'email': account['Email'],
                'status': account['Status'],
                'joined_method': account['JoinedMethod'],
                'joined_timestamp': account['JoinedTimestamp'].isoformat() if account.get('JoinedTimestamp') else None,
                'description': account_tags.get('Description', f"AWS Account: {account['Name']}"),
                'budget': account_tags.get('Budget', 'Not specified'),
                'services': account_tags.get('Services', '').split(',') if account_tags.get('Services') else [],
                'ou_path': ' > '.join(account_ou_path) if account_ou_path else 'Root',
                'parent_id': account_parent,
                'account_type': account_tags.get('Type', account_tags.get('AccountType', 'standard')),
                'is_target_account': account_id == institution_id
            }
            
            if include_aws_details:
                project_data['aws_details'] = {
                    'account_arn': account.get('Arn', ''),
                    'tags': account_tags,
                    'parent_type': 'ROOT' if account_parent == root_id else 'ORGANIZATIONAL_UNIT',
                    'ou_hierarchy': account_ou_path
                }
            
            projects.append(project_data)
        
        # Add organizational units as project containers
        for ou in all_ous:
            ou_id = ou['Id']
            
            # Get OU tags
            try:
                ou_tags_response = orgs_client.list_tags_for_resource(ResourceId=ou_id)
                ou_tags = {tag['Key']: tag['Value'] for tag in ou_tags_response.get('Tags', [])}
            except ClientError:
                ou_tags = {}
            
            # Get accounts in this OU
            try:
                accounts_in_ou_response = orgs_client.list_accounts_for_parent(ParentId=ou_id)
                accounts_in_ou = accounts_in_ou_response.get('Accounts', [])
                account_count = len(accounts_in_ou)
            except ClientError:
                account_count = 0
                accounts_in_ou = []
            
            # Get child OUs count
            child_ous = [child_ou for child_ou in all_ous if child_ou.get('ParentId') == ou_id]
            child_ou_count = len(child_ous)
            
            # Build OU path
            ou_path = [ou['Name']]
            current_parent = ou.get('ParentId')
            while current_parent and current_parent != root_id:
                for parent_ou in all_ous:
                    if parent_ou['Id'] == current_parent:
                        ou_path.insert(0, parent_ou['Name'])
                        current_parent = parent_ou.get('ParentId')
                        break
                else:
                    break
            
            project_data = {
                'id': ou_id,
                'name': ou['Name'],
                'type': 'organizational_unit',
                'description': ou_tags.get('Description', f"Organizational Unit: {ou['Name']}"),
                'budget': ou_tags.get('Budget', 'Not specified'),
                'services': ou_tags.get('Services', '').split(',') if ou_tags.get('Services') else [],
                'level': ou.get('Level', 0),
                'parent_id': ou.get('ParentId'),
                'ou_path': ' > '.join(ou_path),
                'account_count': account_count,
                'child_ou_count': child_ou_count,
                'contains_target_account': institution_id in [acc['Id'] for acc in accounts_in_ou]
            }
            
            if include_aws_details:
                project_data['aws_details'] = {
                    'ou_arn': ou.get('Arn', ''),
                    'tags': ou_tags,
                    'accounts_in_ou': [{'id': acc['Id'], 'name': acc['Name']} for acc in accounts_in_ou],
                    'child_ous': [{'id': child['Id'], 'name': child['Name']} for child in child_ous]
                }
            
            projects.append(project_data)
        
        # Calculate summary statistics
        account_projects = [p for p in projects if p['type'] == 'aws_account']
        ou_projects = [p for p in projects if p['type'] == 'organizational_unit']
        
        total_budget = 0
        budget_specified_count = 0
        
        for project in projects:
            budget_str = project.get('budget', '0')
            if budget_str != 'Not specified':
                try:
                    budget_value = float(budget_str.replace('$', '').replace(',', ''))
                    total_budget += budget_value
                    budget_specified_count += 1
                except (ValueError, AttributeError):
                    pass
        
        # Organization metadata
        org_metadata = {
            'organization_id': organization['Id'],
            'organization_arn': organization['Arn'],
            'master_account_id': organization['MasterAccountId'],
            'master_account_email': organization['MasterAccountEmail'],
            'feature_set': organization['FeatureSet'],
            'root_id': root_id
        }
        
        return {
            "success": True,
            "data": {
                "institution": institution,
                "institution_id": institution_id,
                "institution_name": target_account['Name'],
                "organization": org_metadata,
                "projects": projects,
                "summary": {
                    "total_projects": len(projects),
                    "aws_accounts": len(account_projects),
                    "organizational_units": len(ou_projects),
                    "total_budget": total_budget if budget_specified_count > 0 else "Not calculated",
                    "budget_specified_count": budget_specified_count,
                    "max_ou_level": max([ou.get('Level', 0) for ou in all_ous]) if all_ous else 0
                },
                "aws_details_included": include_aws_details
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in get_projects: {e}")
        return handle_aws_error(e, "get_projects", institution)

# SSO Helper Functions
def _fetch_sso_users(sso_client: Any, identity_store_id: str) -> Dict[str, Any]:
    """Fetch users from AWS SSO Identity Store."""
    users = {}
    
    try:
        # Use identitystore client to search users
        identitystore_client = sso_client  # Assuming this is the identitystore client
        user_list = identitystore_client.list_users(IdentityStoreId=identity_store_id)
        
        for user in user_list.get('Users', []):
            user_id = user['UserId']
            
            # Extract display name
            display_name = user.get('DisplayName', user.get('UserName', 'Unknown'))
            
            # Extract emails and status
            emails = []
            for email in user.get('Emails', []):
                emails.append({
                    'Value': email.get('Value', ''),
                    'Primary': email.get('Primary', False),
                    'Status': 'Verified' if email.get('Primary') else 'Not_Verified'
                })
            
            users[user_id] = {
                'DisplayName': display_name,
                'UserName': user.get('UserName', ''),
                'Emails': emails,
                'Active': True,  # SSO users are typically active
                'Sent': False,
                'Reset': False
            }
            
    except Exception as e:
        logger.error(f"Error fetching SSO users: {e}")
        
    return users

def _fetch_sso_groups(sso_client: Any, identity_store_id: str) -> Dict[str, Any]:
    """Fetch groups and memberships from AWS SSO Identity Store."""
    groups = {}
    
    try:
        # Use identitystore client to list groups
        identitystore_client = sso_client
        group_list = identitystore_client.list_groups(IdentityStoreId=identity_store_id)
        
        for group in group_list.get('Groups', []):
            # Skip AWS managed groups
            if group['DisplayName'].startswith('AWS') and group.get('Description'):
                continue
                
            group_id = group['GroupId']
            
            # Get group memberships
            try:
                memberships = identitystore_client.list_group_memberships(
                    IdentityStoreId=identity_store_id,
                    GroupId=group_id
                )
                
                members = []
                for membership in memberships.get('GroupMemberships', []):
                    member_id = membership.get('MemberId', {})
                    if 'UserId' in member_id:
                        members.append(member_id['UserId'])
                
                groups[group_id] = {
                    'DisplayName': group['DisplayName'],
                    'Description': group.get('Description', ''),
                    'Members': members,
                    'SubGroups': []  # Not implemented in this version
                }
                
            except Exception as e:
                logger.warning(f"Could not get memberships for group {group_id}: {e}")
                groups[group_id] = {
                    'DisplayName': group['DisplayName'],
                    'Description': group.get('Description', ''),
                    'Members': [],
                    'SubGroups': []
                }
                
    except Exception as e:
        logger.error(f"Error fetching SSO groups: {e}")
        
    return groups

def _fetch_sso_assignments(sso_admin_client: Any, sso_arn: str) -> Dict[str, Any]:
    """Fetch permission set assignments from AWS SSO."""
    assignments = {'User': {}, 'Group': {}}
    
    try:
        # List permission sets
        permsets_response = sso_admin_client.list_permission_sets(InstanceArn=sso_arn)
        
        for permset_arn in permsets_response.get('PermissionSets', []):
            try:
                # Get permission set details
                permset_details = sso_admin_client.describe_permission_set(
                    InstanceArn=sso_arn,
                    PermissionSetArn=permset_arn
                )
                
                permset_name = permset_details['PermissionSet'].get('Name', '')
                
                # Only process specific permission sets
                if permset_name not in ['AWSAdministratorAccess', 'OrganizationAdmin', 'StudentAccess']:
                    continue
                
                # List accounts for this permission set
                accounts_response = sso_admin_client.list_accounts_for_provisioned_permission_set(
                    InstanceArn=sso_arn,
                    PermissionSetArn=permset_arn
                )
                
                for account_id in accounts_response.get('AccountIds', []):
                    try:
                        # List account assignments
                        assignments_response = sso_admin_client.list_account_assignments(
                            InstanceArn=sso_arn,
                            AccountId=account_id,
                            PermissionSetArn=permset_arn
                        )
                        
                        for assignment in assignments_response.get('AccountAssignments', []):
                            principal_type = assignment['PrincipalType']
                            principal_id = assignment['PrincipalId']
                            
                            if principal_type == 'GROUP':
                                assignments['Group'].setdefault(principal_id, []).append(account_id)
                            elif principal_type == 'USER':
                                assignments['User'].setdefault(principal_id, []).append(account_id)
                                
                    except Exception as e:
                        logger.warning(f"Could not get assignments for account {account_id}: {e}")
                        
            except Exception as e:
                logger.warning(f"Could not process permission set {permset_arn}: {e}")
                
    except Exception as e:
        logger.error(f"Error fetching SSO assignments: {e}")
        
    return assignments

def _identify_group_owners(users: Dict[str, Any], groups: Dict[str, Any], assignments: Dict[str, Any]) -> Dict[str, List[str]]:
    """Identify group owners based on overlapping account assignments."""
    owners = {}
    
    for group_id, group_accounts in assignments['Group'].items():
        if group_id not in groups:
            continue
            
        group_owners = set()
        
        for user_id, user_accounts in assignments['User'].items():
            if user_id not in users:
                continue
                
            # Check if user has assignments that overlap with group assignments
            if any(account in group_accounts for account in user_accounts):
                group_owners.add(user_id)
                
        owners[group_id] = list(group_owners)
        
    return owners

def _build_user_hierarchy(users: Dict[str, Any], groups: Dict[str, Any], assignments: Dict[str, Any], owners: Dict[str, List[str]]) -> Dict[str, Any]:
    """Build hierarchical user structure with root users and groups."""
    
    # Identify root users (not in groups and not group owners)
    group_owners_set = set()
    for owner_list in owners.values():
        group_owners_set.update(owner_list)
        
    group_members_set = set()
    for group in groups.values():
        group_members_set.update(group['Members'])
    
    root_users = []
    
    for user_id, user in users.items():
        if user_id in group_owners_set or user_id in group_members_set:
            continue
            
        user_accounts = assignments['User'].get(user_id, [])
        user_account_id = ','.join(sorted(user_accounts)) if user_accounts else None
        
        # Extract primary email
        user_email = ''
        user_email_status = 'Not_Verified'
        
        if user['Emails']:
            primary_emails = [e for e in user['Emails'] if e.get('Primary')]
            if primary_emails:
                user_email = primary_emails[0]['Value']
                user_email_status = primary_emails[0]['Status']
            else:
                user_email = user['Emails'][0]['Value']
                user_email_status = user['Emails'][0]['Status']
        
        root_users.append({
            'user_id': user_id,
            'display_name': user['DisplayName'],
            'username': user['UserName'],
            'email': user_email,
            'email_status': user_email_status,
            'account_ids': user_accounts,
            'account_id_string': user_account_id,
            'status': 'Enabled' if user['Active'] else 'Disabled'
        })
    
    # Build group hierarchy
    group_hierarchy = []
    
    for group_id, group in groups.items():
        if group_id not in owners:
            continue
            
        group_accounts = assignments['Group'].get(group_id, [])
        
        # Build owners list
        group_owners_list = []
        for owner_id in owners[group_id]:
            if owner_id not in users:
                continue
                
            owner = users[owner_id]
            owner_accounts = assignments['User'].get(owner_id, [])
            
            # Extract primary email
            owner_email = ''
            owner_email_status = 'Not_Verified'
            
            if owner['Emails']:
                primary_emails = [e for e in owner['Emails'] if e.get('Primary')]
                if primary_emails:
                    owner_email = primary_emails[0]['Value']
                    owner_email_status = primary_emails[0]['Status']
                else:
                    owner_email = owner['Emails'][0]['Value']
                    owner_email_status = owner['Emails'][0]['Status']
            
            group_owners_list.append({
                'user_id': owner_id,
                'display_name': owner['DisplayName'],
                'username': owner['UserName'],
                'email': owner_email,
                'email_status': owner_email_status,
                'account_ids': owner_accounts,
                'status': 'Enabled' if owner['Active'] else 'Disabled',
                'is_owner': True
            })
        
        # Build members list (excluding owners)
        group_members_list = []
        for member_id in group['Members']:
            if member_id in owners[group_id] or member_id not in users:
                continue
                
            member = users[member_id]
            
            # Extract primary email
            member_email = ''
            member_email_status = 'Not_Verified'
            
            if member['Emails']:
                primary_emails = [e for e in member['Emails'] if e.get('Primary')]
                if primary_emails:
                    member_email = primary_emails[0]['Value']
                    member_email_status = primary_emails[0]['Status']
                else:
                    member_email = member['Emails'][0]['Value']
                    member_email_status = member['Emails'][0]['Status']
            
            group_members_list.append({
                'user_id': member_id,
                'display_name': member['DisplayName'],
                'username': member['UserName'],
                'email': member_email,
                'email_status': member_email_status,
                'account_ids': [],
                'status': 'Enabled' if member['Active'] else 'Disabled',
                'is_owner': False
            })
        
        group_hierarchy.append({
            'group_id': group_id,
            'group_name': group['DisplayName'],
            'description': group.get('Description', ''),
            'account_ids': group_accounts,
            'owners': sorted(group_owners_list, key=lambda x: x['display_name']),
            'members': sorted(group_members_list, key=lambda x: x['display_name'])
        })
    
    return {
        'root_users': sorted(root_users, key=lambda x: x['display_name']),
        'groups': sorted(group_hierarchy, key=lambda x: x['group_name'])
    }

@mcp.tool()
def get_users(institution: str, role_filter: Optional[str] = None, include_groups: bool = True, include_assignments: bool = True) -> Dict[str, Any]:
    """
    Get users for a specific institution using AWS SSO/Identity Center approach.
    
    Args:
        institution: Required institution name (sandbox, aueb, grnet)
        role_filter: Optional filter by user roles/permission sets
        include_groups: Include group information (default: True)
        include_assignments: Include account assignments (default: True)
    """
    try:
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Getting SSO users for institution '{institution}', role: {role_filter}, groups: {include_groups}, assignments: {include_assignments}")
        
        # Default region for SSO operations
        region = 'eu-central-1'
        
        # Get SSO Admin client
        sso_admin_client = get_aws_client('sso-admin', institution, region)
        if not sso_admin_client:
            return {
                "success": False,
                "error": f"Could not create AWS SSO Admin client for institution '{institution}'"
            }
        
        # Get Identity Store client
        identitystore_client = get_aws_client('identitystore', institution, region)
        if not identitystore_client:
            return {
                "success": False,
                "error": f"Could not create AWS Identity Store client for institution '{institution}'"
            }
        
        # Get SSO instance information
        try:
            instances_response = sso_admin_client.list_instances()
            if not instances_response.get('Instances'):
                return {
                    "success": False,
                    "error": "No SSO instances found for this institution"
                }
            
            sso_instance = instances_response['Instances'][0]
            sso_instance_id = sso_instance['IdentityStoreId']
            sso_arn = sso_instance['InstanceArn']
            
        except ClientError as e:
            return {
                "success": False,
                "error": f"Could not access SSO instance: {e.response['Error']['Message']}"
            }
        
        # Fetch SSO data
        users = _fetch_sso_users(identitystore_client, sso_instance_id)
        groups = _fetch_sso_groups(identitystore_client, sso_instance_id) if include_groups else {}
        assignments = _fetch_sso_assignments(sso_admin_client, sso_arn) if include_assignments else {'User': {}, 'Group': {}}
        
        # Identify group owners and build hierarchy
        owners = _identify_group_owners(users, groups, assignments) if include_groups else {}
        user_hierarchy = _build_user_hierarchy(users, groups, assignments, owners)
        
        # Apply role filter if specified
        if role_filter:
            # Filter based on permission set assignments or user attributes
            filtered_users = []
            filtered_groups = []
            
            for user in user_hierarchy['root_users']:
                user_assignments = assignments['User'].get(user['user_id'], [])
                # Simple role matching - could be enhanced based on permission set names
                if role_filter.lower() in str(user_assignments).lower():
                    filtered_users.append(user)
            
            for group in user_hierarchy['groups']:
                group_assignments = assignments['Group'].get(group['group_id'], [])
                if role_filter.lower() in str(group_assignments).lower():
                    filtered_groups.append(group)
            
            user_hierarchy['root_users'] = filtered_users
            user_hierarchy['groups'] = filtered_groups
        
        # Calculate summary statistics
        total_users = len(user_hierarchy['root_users'])
        total_groups = len(user_hierarchy['groups'])
        total_assignments = len(assignments['User']) + len(assignments['Group'])
        
        # Add group member counts
        for group in user_hierarchy['groups']:
            total_users += len(group['owners']) + len(group['members'])
        
        return {
            "success": True,
            "data": {
                "institution": institution,
                "sso_instance_id": sso_instance_id,
                "users": user_hierarchy,
                "summary": {
                    "total_users": total_users,
                    "total_groups": total_groups,
                    "total_assignments": total_assignments,
                    "role_filter_applied": role_filter is not None,
                    "groups_included": include_groups,
                    "assignments_included": include_assignments
                }
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in get_users: {e}")
        return handle_aws_error(e, "get_users", institution)

@mcp.tool()
def get_tags(institution: str, resource_arn: str, resource_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Get AWS resource tags for budget and metadata information.
    
    Args:
        institution: Required institution name (sandbox, aueb, grnet)
        resource_arn: Required AWS resource ARN
        resource_type: Optional resource type hint (ec2, s3, rds, etc.)
    """
    try:
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Getting tags for resource: {resource_arn} in institution '{institution}', type: {resource_type}")
        
        # Parse ARN to determine service
        arn_parts = resource_arn.split(':')
        if len(arn_parts) < 6:
            return {
                "success": False,
                "error": "Invalid ARN format"
            }
        
        service = arn_parts[2]
        region = arn_parts[3]
        resource_id = arn_parts[5]
        
        tags = {}
        metadata = {}
        
        # Use Resource Groups Tagging API for general tagging
        try:
            tagging_client = get_aws_client('resourcegroupstaggingapi', institution, region)
            if tagging_client:
                response = tagging_client.get_resources(
                    ResourceARNList=[resource_arn]
                )
                
                if response['ResourceTagMappingList']:
                    resource_info = response['ResourceTagMappingList'][0]
                    tags = {tag['Key']: tag['Value'] for tag in resource_info.get('Tags', [])}
                    
        except ClientError as e:
            logger.warning(f"Could not get tags via Resource Groups API: {e}")
        
        # Try service-specific tagging if Resource Groups API fails
        if not tags:
            try:
                if service == 'ec2':
                    ec2_client = get_aws_client('ec2', institution, region)
                    if ec2_client:
                        response = ec2_client.describe_tags(
                            Filters=[
                                {'Name': 'resource-id', 'Values': [resource_id]}
                            ]
                        )
                        tags = {tag['Key']: tag['Value'] for tag in response.get('Tags', [])}
                        
                elif service == 's3':
                    s3_client = get_aws_client('s3', institution, region)
                    if s3_client:
                        bucket_name = resource_id.split('/')[0]
                        response = s3_client.get_bucket_tagging(Bucket=bucket_name)
                        tags = {tag['Key']: tag['Value'] for tag in response.get('TagSet', [])}
                        
                elif service == 'organizations':
                    orgs_client = get_aws_client('organizations', institution)
                    if orgs_client:
                        response = orgs_client.list_tags_for_resource(ResourceId=resource_id)
                        tags = {tag['Key']: tag['Value'] for tag in response.get('Tags', [])}
                        
            except ClientError as e:
                logger.warning(f"Could not get tags via {service} API: {e}")
        
        # Extract budget and metadata from tags
        budget_info = {}
        if 'Budget' in tags:
            budget_info['budget'] = tags['Budget']
        if 'CostCenter' in tags:
            budget_info['cost_center'] = tags['CostCenter']
        if 'Project' in tags:
            budget_info['project'] = tags['Project']
        if 'Owner' in tags:
            budget_info['owner'] = tags['Owner']
        
        # Build metadata
        metadata = {
            'institution': institution,
            'resource_arn': resource_arn,
            'service': service,
            'region': region,
            'resource_type': resource_type or service,
            'tag_count': len(tags),
            'budget_info': budget_info,
            'has_budget_tags': bool(budget_info)
        }
        
        return {
            "success": True,
            "data": {
                "institution": institution,
                "resource_arn": resource_arn,
                "tags": tags,
                "metadata": metadata,
                "budget_info": budget_info
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in get_tags: {e}")
        return handle_aws_error(e, "get_tags", institution)

# Budget Monitoring Helper Functions
def _get_date_range(period: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None) -> tuple:
    """Helper function to get date range for cost analysis."""
    today = datetime.now(timezone.utc).date()
    
    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date.replace('Z', '+00:00')).date()
            end = datetime.fromisoformat(end_date.replace('Z', '+00:00')).date()
            return start, end
        except ValueError:
            # Fallback to string parsing
            start = datetime.strptime(start_date[:10], '%Y-%m-%d').date()
            end = datetime.strptime(end_date[:10], '%Y-%m-%d').date()
            return start, end
    
    if period == "past_month":
        # Previous month
        first_day_current = today.replace(day=1)
        last_day_previous = first_day_current - timedelta(days=1)
        first_day_previous = last_day_previous.replace(day=1)
        return first_day_previous, last_day_previous
    elif period == "current_month":
        # Current month to date
        first_day_current = today.replace(day=1)
        return first_day_current, today
    else:
        # Default: current month to date
        first_day_current = today.replace(day=1)
        return first_day_current, today

def _validate_date_range(start_date: date, end_date: date) -> bool:
    """Validate date range for cost analysis."""
    if start_date > end_date:
        return False
    
    # AWS Cost Explorer has limitations
    today = datetime.now(timezone.utc).date()
    if end_date > today:
        return False
    
    # Check if date range is not too far in the past (AWS keeps ~13 months)
    max_past_date = today - timedelta(days=365)
    if start_date < max_past_date:
        return False
    
    return True

def _fetch_cost_explorer_data(ce_client: Any, account_ids: List[str], start_date: date,
                            end_date: date, granularity: str = "DAILY",
                            exclude_services: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetch cost data from AWS Cost Explorer."""
    if exclude_services is None:
        exclude_services = ["Tax"]
    
    try:
        # Build filter for excluding services
        filters = {}
        if exclude_services:
            filters["Not"] = {
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": exclude_services
                }
            }
        
        # If specific account IDs provided, filter by them
        if account_ids:
            account_filter = {
                "Dimensions": {
                    "Key": "LINKED_ACCOUNT",
                    "Values": account_ids
                }
            }
            
            if filters:
                filters = {
                    "And": [filters, account_filter]
                }
            else:
                filters = account_filter
        
        query_params = {
            "TimePeriod": {
                "Start": start_date.strftime("%Y-%m-%d"),
                "End": end_date.strftime("%Y-%m-%d")
            },
            "Granularity": granularity,
            "Metrics": ["UnblendedCost"],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"}
            ]
        }
        
        if filters:
            query_params["Filter"] = filters
        
        # Handle pagination
        all_results = []
        next_token = None
        
        while True:
            if next_token:
                query_params["NextPageToken"] = next_token
            
            response = ce_client.get_cost_and_usage(**query_params)
            all_results.extend(response.get("ResultsByTime", []))
            
            next_token = response.get("NextPageToken")
            if not next_token:
                break
        
        return {"ResultsByTime": all_results}
        
    except ClientError as e:
        logger.error(f"Error fetching cost data: {e}")
        raise

def _process_cost_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Process AWS Cost Explorer response into structured cost data."""
    processed_costs = {}
    
    for result in raw_data.get("ResultsByTime", []):
        date = result["TimePeriod"]["Start"]
        
        for group in result.get("Groups", []):
            account_id = group["Keys"][0]
            service = group["Keys"][1]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            currency = group["Metrics"]["UnblendedCost"]["Unit"]
            
            if account_id not in processed_costs:
                processed_costs[account_id] = {
                    "total_cost": 0.0,
                    "currency": currency,
                    "services": {},
                    "daily_costs": {}
                }
            
            # Add to total cost
            processed_costs[account_id]["total_cost"] += amount
            
            # Add to service breakdown
            if service not in processed_costs[account_id]["services"]:
                processed_costs[account_id]["services"][service] = 0.0
            processed_costs[account_id]["services"][service] += amount
            
            # Add to daily breakdown
            if date not in processed_costs[account_id]["daily_costs"]:
                processed_costs[account_id]["daily_costs"][date] = 0.0
            processed_costs[account_id]["daily_costs"][date] += amount
    
    return processed_costs

def _get_project_budgets(institution: str, project_ids: List[str]) -> Dict[str, float]:
    """Extract budgets from project tags/metadata using existing get_projects function."""
    budgets = {}
    
    try:
        # Get projects data for the institution
        # We need to find the master account ID for this institution
        orgs_client = get_aws_client('organizations', institution)
        if not orgs_client:
            return budgets
        
        # Get organization info to find master account
        org_response = orgs_client.describe_organization()
        master_account_id = org_response['Organization']['MasterAccountId']
        
        # Get all accounts to find budgets
        accounts_response = orgs_client.list_accounts()
        accounts = accounts_response['Accounts']
        
        for account in accounts:
            account_id = account['Id']
            if account_id in project_ids:
                try:
                    # Get account tags
                    tags_response = orgs_client.list_tags_for_resource(ResourceId=account_id)
                    tags = {tag['Key']: tag['Value'] for tag in tags_response.get('Tags', [])}
                    
                    # Extract budget from tags
                    budget_str = tags.get('Budget', '0')
                    if budget_str and budget_str != 'Not specified':
                        try:
                            # Clean budget string and convert to float
                            budget_value = float(budget_str.replace('$', '').replace(',', ''))
                            budgets[account_id] = budget_value
                        except (ValueError, AttributeError):
                            budgets[account_id] = 0.0
                    else:
                        budgets[account_id] = 0.0
                        
                except ClientError:
                    budgets[account_id] = 0.0
    
    except Exception as e:
        logger.error(f"Error getting project budgets: {e}")
    
    return budgets

def _analyze_project_budgets(costs: Dict[str, Any], budgets: Dict[str, float]) -> Dict[str, Any]:
    """Compare costs vs budgets and identify overbudget projects."""
    analysis = {
        "projects": [],
        "overbudget_projects": [],
        "summary": {
            "total_cost": 0.0,
            "total_budget": 0.0,
            "overbudget_count": 0,
            "total_projects": 0
        }
    }
    
    for account_id, cost_data in costs.items():
        budget = budgets.get(account_id, 0.0)
        actual_cost = cost_data["total_cost"]
        
        # Calculate budget utilization
        budget_utilization = (actual_cost / budget * 100) if budget > 0 else 0.0
        budget_status = "overbudget" if actual_cost > budget and budget > 0 else "within_budget"
        
        # Get top services by cost
        services = cost_data.get("services", {})
        top_services = sorted(services.items(), key=lambda x: x[1], reverse=True)[:5]
        
        project_analysis = {
            "project_id": account_id,
            "budget": budget,
            "actual_cost": actual_cost,
            "budget_status": budget_status,
            "budget_utilization": round(budget_utilization, 2),
            "remaining_budget": max(0, budget - actual_cost),
            "overage": max(0, actual_cost - budget),
            "currency": cost_data.get("currency", "USD"),
            "cost_breakdown": [
                {"service": service, "cost": round(cost, 2)}
                for service, cost in top_services
            ]
        }
        
        analysis["projects"].append(project_analysis)
        
        # Add to overbudget list if applicable
        if budget_status == "overbudget":
            analysis["overbudget_projects"].append(project_analysis)
            analysis["summary"]["overbudget_count"] += 1
        
        # Update summary
        analysis["summary"]["total_cost"] += actual_cost
        analysis["summary"]["total_budget"] += budget
        analysis["summary"]["total_projects"] += 1
    
    # Round summary values
    analysis["summary"]["total_cost"] = round(analysis["summary"]["total_cost"], 2)
    analysis["summary"]["total_budget"] = round(analysis["summary"]["total_budget"], 2)
    analysis["summary"]["budget_utilization"] = round(
        (analysis["summary"]["total_cost"] / analysis["summary"]["total_budget"] * 100)
        if analysis["summary"]["total_budget"] > 0 else 0.0, 2
    )
    analysis["summary"]["remaining_budget"] = round(
        max(0, analysis["summary"]["total_budget"] - analysis["summary"]["total_cost"]), 2
    )
    
    return analysis

def _calculate_institution_costs(costs: Dict[str, Any], projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate costs by institution."""
    total_cost = sum(cost_data["total_cost"] for cost_data in costs.values())
    total_budget = sum(project.get("budget", 0.0) for project in projects)
    
    # Aggregate services across all projects
    all_services = {}
    for cost_data in costs.values():
        for service, cost in cost_data.get("services", {}).items():
            if service not in all_services:
                all_services[service] = 0.0
            all_services[service] += cost
    
    # Get top services
    top_services = sorted(all_services.items(), key=lambda x: x[1], reverse=True)[:10]
    
    return {
        "total_cost": round(total_cost, 2),
        "total_budget": round(total_budget, 2),
        "remaining_budget": round(max(0, total_budget - total_cost), 2),
        "budget_utilization": round((total_cost / total_budget * 100) if total_budget > 0 else 0.0, 2),
        "top_services": [
            {"service": service, "cost": round(cost, 2)}
            for service, cost in top_services
        ]
    }

def _identify_overbudget_projects(cost_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract and format overbudget projects."""
    return cost_analysis.get("overbudget_projects", [])

@mcp.tool()
def check_budget(institution: str, project_id: Optional[str] = None, start_date: Optional[str] = None,
                end_date: Optional[str] = None, exclude_services: Optional[List[str]] = None,
                granularity: str = "DAILY", budget_check: bool = True, period: Optional[str] = None) -> Dict[str, Any]:
    """
    Comprehensive budget monitoring tool with AWS Cost Explorer integration.
    
    Args:
        institution: Required institution name (sandbox, aueb, grnet)
        project_id: Optional specific project/account ID
        start_date: Optional start date for cost analysis (ISO format)
        end_date: Optional end date for cost analysis (ISO format)
        exclude_services: Optional services to exclude (default: ["Tax"])
        granularity: DAILY or MONTHLY (default: DAILY)
        budget_check: Check budget vs actual costs (default: true)
        period: Optional period shortcut ("past_month", "current_month")
    """
    try:
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        # Set default exclude services
        if exclude_services is None:
            exclude_services = ["Tax"]
        
        # Get date range
        try:
            analysis_start, analysis_end = _get_date_range(period, start_date, end_date)
        except Exception as e:
            return {
                "success": False,
                "error": f"Invalid date range: {str(e)}"
            }
        
        # Validate date range
        if not _validate_date_range(analysis_start, analysis_end):
            return {
                "success": False,
                "error": "Invalid date range. End date must be after start date and not in the future."
            }
        
        logger.info(f"Checking budget for institution '{institution}', project: {project_id}, "
                   f"period: {analysis_start} to {analysis_end}")
        
        # Get Cost Explorer client
        ce_client = get_aws_client('ce', institution, 'us-east-1')
        if not ce_client:
            return {
                "success": False,
                "error": f"Could not create AWS Cost Explorer client for institution '{institution}'"
            }
        
        # Get organization client for project data
        orgs_client = get_aws_client('organizations', institution)
        if not orgs_client:
            return {
                "success": False,
                "error": f"Could not create AWS Organizations client for institution '{institution}'"
            }
        
        # Get account IDs to analyze
        account_ids = []
        if project_id:
            account_ids = [project_id]
        else:
            # Get all accounts in the organization
            try:
                accounts_response = orgs_client.list_accounts()
                account_ids = [acc['Id'] for acc in accounts_response['Accounts']]
            except ClientError as e:
                return {
                    "success": False,
                    "error": f"Could not list organization accounts: {e.response['Error']['Message']}"
                }
        
        # Fetch cost data
        try:
            raw_cost_data = _fetch_cost_explorer_data(
                ce_client, account_ids, analysis_start, analysis_end, granularity, exclude_services
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch cost data: {str(e)}"
            }
        
        # Process cost data
        processed_costs = _process_cost_data(raw_cost_data)
        
        # Get project budgets if budget check is enabled
        budgets = {}
        if budget_check:
            budgets = _get_project_budgets(institution, account_ids)
        
        # Analyze budgets vs costs
        cost_analysis = _analyze_project_budgets(processed_costs, budgets)
        
        # Get project names and metadata
        project_metadata = {}
        try:
            accounts_response = orgs_client.list_accounts()
            for account in accounts_response['Accounts']:
                if account['Id'] in account_ids:
                    project_metadata[account['Id']] = {
                        "name": account['Name'],
                        "email": account['Email'],
                        "status": account['Status']
                    }
        except ClientError:
            pass
        
        # Enhance project data with names
        for project in cost_analysis["projects"]:
            project_id_key = project["project_id"]
            if project_id_key in project_metadata:
                project["project_name"] = project_metadata[project_id_key]["name"]
                project["project_email"] = project_metadata[project_id_key]["email"]
                project["project_status"] = project_metadata[project_id_key]["status"]
            else:
                project["project_name"] = f"Account-{project_id_key}"
        
        # Calculate institution totals
        institution_totals = _calculate_institution_costs(processed_costs, cost_analysis["projects"])
        
        # Build response
        response_data = {
            "institution": institution,
            "analysis_period": {
                "start_date": analysis_start.isoformat(),
                "end_date": analysis_end.isoformat(),
                "granularity": granularity,
                "excluded_services": exclude_services
            },
            "summary": cost_analysis["summary"],
            "projects": cost_analysis["projects"],
            "institution_totals": institution_totals
        }
        
        # Add overbudget projects if any
        if cost_analysis["overbudget_projects"]:
            response_data["overbudget_projects"] = cost_analysis["overbudget_projects"]
        
        # Add specific project focus if requested
        if project_id and project_id in processed_costs:
            response_data["focused_project"] = {
                "project_id": project_id,
                "project_name": project_metadata.get(project_id, {}).get("name", f"Account-{project_id}"),
                "cost_details": processed_costs[project_id]
            }
        
        return {
            "success": True,
            "data": response_data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error in check_budget: {e}")
        return handle_aws_error(e, "check_budget", institution)

# SSO Helper Functions for Email Verification and Password Reset
def _create_sso_client(institution: str) -> Optional[Any]:
    """Create SingleSignOnClient with institution credentials."""
    if not INIA_AVAILABLE:
        logger.error("inia module not available for SSO operations")
        return None
    
    try:
        # Get institution-specific credentials
        credentials = get_institution_credentials(institution)
        if not credentials:
            logger.error(f"Could not get credentials for institution '{institution}'")
            return None
        
        # Create SingleSignOnClient with institution-specific credentials
        sso_client = SingleSignOnClient(
            access_key=credentials['aws_access_key_id'],
            secret_key=credentials['aws_secret_access_key'],
            region='eu-central-1'  # Default region for SSO operations
        )
        
        logger.info(f"Created SSO client for institution '{institution}'")
        return sso_client
        
    except Exception as e:
        logger.error(f"Failed to create SSO client for institution '{institution}': {e}")
        return None

def _get_identity_store_id(institution: str) -> Optional[str]:
    """Get identity store ID for institution."""
    try:
        # Get SSO Admin client to find the identity store ID
        sso_admin_client = get_aws_client('sso-admin', institution, 'eu-central-1')
        if not sso_admin_client:
            logger.error(f"Could not create SSO Admin client for institution '{institution}'")
            return None
        
        # Get SSO instance information
        instances_response = sso_admin_client.list_instances()
        if not instances_response.get('Instances'):
            logger.error(f"No SSO instances found for institution '{institution}'")
            return None
        
        sso_instance = instances_response['Instances'][0]
        identity_store_id = sso_instance['IdentityStoreId']
        
        logger.info(f"Found identity store ID '{identity_store_id}' for institution '{institution}'")
        return identity_store_id
        
    except Exception as e:
        logger.error(f"Failed to get identity store ID for institution '{institution}': {e}")
        return None

def _find_user_by_identifier(institution: str, user_identifier: str) -> Optional[Dict[str, Any]]:
    """Find user ID by email/display name using AWS Identity Store directly."""
    try:
        logger.info(f"Searching for user '{user_identifier}' in institution '{institution}'")
        
        # Get identity store ID
        identity_store_id = _get_identity_store_id(institution)
        if not identity_store_id:
            logger.error(f"Could not get identity store ID for institution '{institution}'")
            return None
        
        # Get Identity Store client
        identitystore_client = get_aws_client('identitystore', institution, 'eu-central-1')
        if not identitystore_client:
            logger.error(f"Could not create Identity Store client for institution '{institution}'")
            return None
        
        # Search users by email first
        try:
            # Try to search by email
            users_response = identitystore_client.list_users(IdentityStoreId=identity_store_id)
            users = users_response.get('Users', [])
            
            for user in users:
                user_id = user['UserId']
                display_name = user.get('DisplayName', user.get('UserName', ''))
                username = user.get('UserName', '')
                
                # Extract primary email
                user_email = ''
                for email in user.get('Emails', []):
                    if email.get('Primary', False):
                        user_email = email.get('Value', '')
                        break
                if not user_email and user.get('Emails'):
                    user_email = user['Emails'][0].get('Value', '')
                
                # Check if identifier matches email, display name, or username
                if (user_identifier.lower() in user_email.lower() or
                    user_identifier.lower() in display_name.lower() or
                    user_identifier.lower() in username.lower() or
                    user_identifier == user_id):
                    
                    return {
                        'user_id': user_id,
                        'display_name': display_name,
                        'email': user_email,
                        'username': username
                    }
            
            logger.warning(f"User '{user_identifier}' not found in institution '{institution}'")
            return None
            
        except ClientError as e:
            logger.error(f"AWS API error while searching for user: {e}")
            return None
        
    except Exception as e:
        logger.error(f"Error searching for user '{user_identifier}' in institution '{institution}': {e}")
        return None

@mcp.tool()
def verify_email(institution: str, user_identifier: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Verify email for a user using AWS SSO Identity Center.
    
    Args:
        institution: Required institution name for credential selection
        user_identifier: Required user email or display name to find the user
        user_id: Optional direct AWS SSO user ID if known
    """
    try:
        # Check if inia module is available
        if not INIA_AVAILABLE:
            return {
                "success": False,
                "error": "inia module not available. Please install inia>=1.0.0 to use email verification."
            }
        
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Starting email verification for user '{user_identifier}' in institution '{institution}'")
        
        # Find user if user_id not provided
        user_info = None
        if not user_id:
            user_info = _find_user_by_identifier(institution, user_identifier)
            if not user_info:
                return {
                    "success": False,
                    "error": f"User '{user_identifier}' not found in institution '{institution}'"
                }
            user_id = user_info['user_id']
        
        # Get identity store ID
        identity_store_id = _get_identity_store_id(institution)
        if not identity_store_id:
            return {
                "success": False,
                "error": f"Could not get identity store ID for institution '{institution}'"
            }
        
        # Create SSO client
        sso_client = _create_sso_client(institution)
        if not sso_client:
            return {
                "success": False,
                "error": f"Could not create SSO client for institution '{institution}'"
            }
        
        # Perform email verification
        try:
            aws_response = sso_client.verify_email(user_id, identity_store_id)
            
            return {
                "success": True,
                "data": {
                    "institution": institution,
                    "user_id": user_id,
                    "user_identifier": user_identifier,
                    "operation": "verify_email",
                    "aws_response": sanitize_aws_response(aws_response),
                    "message": "Email verification initiated successfully",
                    "user_info": user_info
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"AWS API error during email verification: {e}")
            return {
                "success": False,
                "error": f"Email verification failed: {str(e)}",
                "institution": institution,
                "user_id": user_id,
                "operation": "verify_email"
            }
        
    except Exception as e:
        logger.error(f"Error in verify_email: {e}")
        return handle_aws_error(e, "verify_email", institution)

@mcp.tool()
def reset_password(institution: str, user_identifier: str, user_id: Optional[str] = None, mode: str = "EMAIL") -> Dict[str, Any]:
    """
    Reset password for a user using AWS SSO Identity Center.
    
    Args:
        institution: Required institution name for credential selection
        user_identifier: Required user email or display name to find the user
        user_id: Optional direct AWS SSO user ID if known
        mode: Optional password reset mode, defaults to "EMAIL"
    """
    try:
        # Check if inia module is available
        if not INIA_AVAILABLE:
            return {
                "success": False,
                "error": "inia module not available. Please install inia>=1.0.0 to use password reset."
            }
        
        # Validate institution
        if not validate_institution(institution):
            return {
                "success": False,
                "error": f"Institution '{institution}' not found. Available institutions: {get_available_institutions()}"
            }
        
        logger.info(f"Starting password reset for user '{user_identifier}' in institution '{institution}' with mode '{mode}'")
        
        # Find user if user_id not provided
        user_info = None
        if not user_id:
            user_info = _find_user_by_identifier(institution, user_identifier)
            if not user_info:
                return {
                    "success": False,
                    "error": f"User '{user_identifier}' not found in institution '{institution}'"
                }
            user_id = user_info['user_id']
        
        # Create SSO client
        sso_client = _create_sso_client(institution)
        if not sso_client:
            return {
                "success": False,
                "error": f"Could not create SSO client for institution '{institution}'"
            }
        
        # Perform password reset
        try:
            aws_response = sso_client.update_password(user_id, mode)
            
            return {
                "success": True,
                "data": {
                    "institution": institution,
                    "user_id": user_id,
                    "user_identifier": user_identifier,
                    "operation": "reset_password",
                    "mode": mode,
                    "aws_response": sanitize_aws_response(aws_response),
                    "message": f"Password reset initiated successfully with mode '{mode}'",
                    "user_info": user_info
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"AWS API error during password reset: {e}")
            return {
                "success": False,
                "error": f"Password reset failed: {str(e)}",
                "institution": institution,
                "user_id": user_id,
                "operation": "reset_password",
                "mode": mode
            }
        
    except Exception as e:
        logger.error(f"Error in reset_password: {e}")
        return handle_aws_error(e, "reset_password", institution)

# Initialize server
def initialize_server():
    """Initialize the server with multi-institution AWS credentials and resources."""
    try:
        logger.info("Initializing Real AWS Organizations MCP Server with multi-institution support...")
        
        # Load multi-institution AWS credentials
        initialize_aws_credentials()
        
        # Register institution resources
        register_institution_resources()
        
        available_institutions = get_available_institutions()
        logger.info(f"Server initialization completed successfully with {len(available_institutions)} institutions: {available_institutions}")
        
    except Exception as e:
        logger.error(f"Server initialization failed: {e}")
        raise

if __name__ == "__main__":
    logger.info("Starting Real AWS Organizations MCP Server with multi-institution support in STDIO mode...")
    
    try:
        # Initialize server
        initialize_server()
        
        # Run in STDIO mode for VSCode Roo plugin integration
        logger.info("Starting MCP server with STDIO transport for VSCode Roo...")
        mcp.run(transport="stdio")
        
    except Exception as e:
        logger.error(f"Failed to start MCP server: {e}")
