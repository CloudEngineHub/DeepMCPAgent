"""Acme platform admin API, the server the experiment measures.

14 resources x 5 standard tools (list, get, create, update, delete) + 52 actions = 122 tools.
Every tool echoes its arguments back. Descriptions say when to use a tool rather than its
look-alike, the way a careful API team would write them.

Run it over stdio (the experiment scripts start it for you): python platform_server.py
"""
from promptise.mcp.server import MCPServer

from acme_tools import add_tools

server = MCPServer("acme-platform")

# resource: (plural, ID example, what it is, fields you can set on create and update)
RESOURCES = {
    "user": ("users", "u_42", "a person with a login in your organization",
             {"email": "Work email address.", "role": "One of owner, admin, member, billing or viewer."}),
    "team": ("teams", "t_7", "a group of users that share access to projects",
             {"name": "Display name of the team.", "description": "What the team works on."}),
    "project": ("projects", "prj_web_shop", "an app with its own deployments, environments and domains",
                {"name": "Project name.", "framework": "Framework preset, for example nextjs or django."}),
    "deployment": ("deployments", "dep_9f2c", "one build of a project released to an environment",
                   {"project_id": "The project to deploy.", "git_ref": "Branch, tag or commit SHA to build."}),
    "environment": ("environments", "env_prod", "a deployment target of a project, such as production or staging",
                    {"name": "Environment name.", "branch": "Git branch that deploys here automatically."}),
    "domain": ("domains", "acme.io", "a domain name your organization owns",
               {"hostname": "Fully qualified domain name.", "project_id": "Project that serves it."}),
    "dns_record": ("DNS records", "rec_118", "one A, AAAA, CNAME, MX or TXT record in a domain's zone",
                   {"domain_id": "The domain whose zone gets the record.", "type": "Record type: A, AAAA, CNAME, MX or TXT.",
                    "name": "Record name, for example www.", "value": "Record value, for example an IP address or a hostname."}),
    "api_key": ("API keys", "key_8812", "a secret that lets a program call the API",
                {"name": "Label for the key.", "scopes": "Comma-separated permissions."}),
    "webhook": ("webhooks", "wh_3", "an HTTPS endpoint that receives event notifications",
                {"url": "HTTPS URL that receives events.", "events": "Comma-separated event types."}),
    "feature_flag": ("feature flags", "new_checkout", "a switch that turns a feature on for some or all users",
                     {"key": "Flag key used in code.", "description": "What the flag controls."}),
    "bucket": ("storage buckets", "assets-prod", "an object storage bucket for files",
               {"name": "Bucket name.", "region": "Region code, for example eu-west-1."}),
    "database": ("databases", "db_staging", "a managed PostgreSQL database",
                 {"name": "Database name.", "plan": "Size plan: small, medium, large or xlarge."}),
    "alert_rule": ("alert rules", "ar_5", "a rule that pages someone when a metric crosses a threshold",
                   {"metric": "Metric name.", "threshold": "Value that triggers the alert.", "notify": "Who gets paged."}),
    "incident": ("incidents", "inc_12", "an outage or degradation that someone is working on",
                 {"title": "Short incident title.", "severity": "sev1, sev2 or sev3."}),
}

# Actions beyond list, get, create, update and delete. name: (description, {parameter: description})
ACTIONS = {
    # Users and teams
    "invite_user": ("Send someone an email invitation to join the organization with a role. "
                    "Use this, not create_user, for people who don't have an account yet.",
                    {"email": "Email address to invite.", "role": "Role to grant when they accept."}),
    "suspend_user": ("Suspend a user so they can't sign in. Their data and memberships are kept and can be restored. "
                     "Use this, not delete_user, when someone leaves the company.",
                     {"user_id": "The user to suspend.", "reason": "Why, recorded in the audit log."}),
    "reactivate_user": ("Lift a suspension so the user can sign in again, with their old memberships.",
                        {"user_id": "The suspended user."}),
    "revoke_user_sessions": ("Sign a user out everywhere by ending all their active sessions and tokens. "
                             "They can sign in again; use it when a device is lost or an account may be compromised.",
                             {"user_id": "The user to sign out."}),
    "reset_user_mfa": ("Remove a user's two-factor devices so they can enroll a new one, for example after losing a phone.",
                       {"user_id": "The user whose devices to remove."}),
    "enforce_mfa": ("Require two-factor authentication for every member of the organization.",
                    {"grace_days": "Optional days members have to enroll before they are locked out."}),
    "transfer_ownership": ("Make another admin the owner of the organization. The current owner becomes an admin.",
                           {"user_id": "The new owner."}),
    "add_team_member": ("Add an existing user to a team, giving them the team's project access.",
                        {"team_id": "The team.", "user_id": "The user to add."}),
    "remove_team_member": ("Remove a user from a team. They keep their account and other teams.",
                           {"team_id": "The team.", "user_id": "The user to remove."}),
    # Projects and deployments
    "archive_project": ("Archive a project: it stops deploying and becomes read-only, but nothing is deleted. "
                        "Use this, not delete_project, for projects you might need again.",
                        {"project_id": "The project to archive."}),
    "transfer_project": ("Move a project to another team.", {"project_id": "The project.", "team_id": "The team that gets it."}),
    "rollback_deployment": ("Put a project's production back to the previous successful deployment, "
                            "for example when the latest release broke something.",
                            {"project_id": "The project to roll back."}),
    "promote_deployment": ("Release an existing preview deployment to production without rebuilding it.",
                           {"deployment_id": "The preview deployment to release."}),
    "cancel_deployment": ("Stop a deployment that is still building or queued. Finished deployments can't be cancelled.",
                          {"deployment_id": "The deployment to stop."}),
    "redeploy": ("Build and deploy the same commit again, for example after changing an environment variable.",
                 {"deployment_id": "The deployment to repeat."}),
    "get_deployment_logs": ("Get the build and runtime log lines of one deployment.",
                            {"deployment_id": "The deployment.", "lines": "Optional number of lines from the end."}),
    "set_environment_variable": ("Set an environment variable, such as an API secret, on one environment of a project. "
                                 "It applies from the next deployment.",
                                 {"environment_id": "The environment.", "key": "Variable name.", "value": "Variable value."}),
    "unset_environment_variable": ("Remove an environment variable from one environment of a project.",
                                   {"environment_id": "The environment.", "key": "Variable name."}),
    # Domains
    "verify_domain": ("Check whether a domain's verification TXT record is in place and mark the domain verified.",
                      {"domain_id": "The domain to check."}),
    "renew_certificate": ("Renew the TLS certificate of a domain now instead of waiting for automatic renewal.",
                          {"domain_id": "The domain."}),
    "set_primary_domain": ("Make a domain the one a project redirects all its other domains to.",
                           {"project_id": "The project.", "domain_id": "The domain to make primary."}),
    # API keys
    "rotate_api_key": ("Issue a new secret for an API key and retire the old one. The key keeps its ID, name and permissions. "
                       "Use this, not create_api_key, when a secret has leaked.",
                       {"api_key_id": "The key to rotate."}),
    "get_api_key_usage": ("Count the requests made with an API key, per day, with error rates.",
                          {"api_key_id": "The key.", "days": "Optional number of days back, 7 by default."}),
    "disable_api_key": ("Turn an API key off without deleting it. enable_api_key turns it back on.",
                        {"api_key_id": "The key to turn off."}),
    "enable_api_key": ("Turn a disabled API key back on.", {"api_key_id": "The key to turn on."}),
    # Webhooks
    "list_webhook_deliveries": ("List recent deliveries of a webhook, with status codes, so you can find failed ones.",
                                {"webhook_id": "The webhook.", "status": "Optional failed or succeeded."}),
    "retry_webhook_delivery": ("Send one past webhook delivery again, for example one that failed or was missed.",
                               {"delivery_id": "The delivery to send again."}),
    "send_test_webhook": ("Send a test event to a webhook to check that the endpoint answers.",
                          {"webhook_id": "The webhook."}),
    # Feature flags
    "toggle_feature_flag": ("Turn a feature flag fully on or fully off for everyone.",
                            {"feature_flag_id": "The flag.", "enabled": "true or false."}),
    "set_feature_flag_rollout": ("Turn a feature flag on for a percentage of users, for a gradual rollout.",
                                 {"feature_flag_id": "The flag.", "percent": "Share of users, 0 to 100."}),
    # Buckets
    "empty_bucket": ("Delete every file in a storage bucket but keep the bucket.", {"bucket_id": "The bucket."}),
    "create_signed_url": ("Create a temporary download link for one file in a bucket.",
                          {"bucket_id": "The bucket.", "path": "Path of the file.", "expires_minutes": "Optional link lifetime."}),
    # Databases
    "create_database_backup": ("Take a backup of a database now, in addition to the nightly automatic backups.",
                               {"database_id": "The database."}),
    "list_database_backups": ("List a database's backups, newest first, with their IDs and times.",
                              {"database_id": "The database."}),
    "restore_database_backup": ("Restore a database to the state of one of its backups. Changes made since are lost.",
                                {"database_id": "The database.", "backup_id": "The backup to restore."}),
    "resize_database": ("Move a database to a bigger or smaller plan, for example when it runs out of memory.",
                        {"database_id": "The database.", "plan": "small, medium, large or xlarge."}),
    "rotate_database_password": ("Generate a new password for a database and update the connection string.",
                                 {"database_id": "The database."}),
    "run_read_only_query": ("Run a read-only SQL query against a database and return the rows.",
                            {"database_id": "The database.", "sql": "A SELECT statement."}),
    # Alerts and incidents
    "mute_alert_rule": ("Silence an alert rule for a while, for example during planned maintenance. It still records data.",
                        {"alert_rule_id": "The rule.", "minutes": "How long to stay silent."}),
    "test_alert_rule": ("Fire a test page for an alert rule to check who receives it.", {"alert_rule_id": "The rule."}),
    "acknowledge_incident": ("Acknowledge an incident so people know someone is on it. It stays open.",
                             {"incident_id": "The incident."}),
    "resolve_incident": ("Mark an incident resolved when the problem is fixed. This closes it.",
                         {"incident_id": "The incident.", "summary": "Optional what fixed it."}),
    "escalate_incident": ("Page the next level of on-call for an incident.", {"incident_id": "The incident."}),
    "post_incident_update": ("Post a status update on an incident's public status page.",
                             {"incident_id": "The incident.", "message": "Text of the update."}),
    # Organization
    "list_audit_events": ("Search the audit log: who did what, and when, across the organization. "
                          "Use it to find out who changed or deleted something.",
                          {"action": "Action to filter by, for example database.delete.", "since_days": "How far back."}),
    "export_audit_log": ("Export the audit log for a date range as a CSV file for a compliance review. "
                         "To answer a question about who did something, use list_audit_events.",
                         {"from_date": "First day, YYYY-MM-DD.", "to_date": "Last day, YYYY-MM-DD."}),
    "list_regions": ("List the regions where you can create databases, buckets and deployments.", {}),
    "search_logs": ("Search the application logs of all projects for a text or an error message.",
                    {"query": "Text to search for.", "since_hours": "Optional how far back."}),
    "get_organization": ("Get the organization's name, plan, owner and limits.", {}),
    "update_organization_settings": ("Change organization settings such as its name or default region.",
                                     {"name": "Optional new name.", "default_region": "Optional region code."}),
    "request_quota_increase": ("Ask Acme support to raise one of the organization's limits, such as the number of projects.",
                               {"quota": "Which limit.", "amount": "The limit you need.", "reason": "Optional why."}),
    "get_usage_report": ("Get this month's usage of the organization: build minutes, bandwidth, storage and requests.",
                         {"month": "Optional month as YYYY-MM."}),
}

add_tools(server, RESOURCES, ACTIONS)

if __name__ == "__main__":
    server.run()
