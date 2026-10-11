"""Twenty admin requests, phrased the way people ask, each with the one tool that does the job."""

TASKS = [
    ("Jane (u_42) left the company yesterday. Make sure she can't get in any more.", "suspend_user"),
    ("The last deploy of prj_web_shop broke checkout. Put production back the way it was.", "rollback_deployment"),
    ("Our CI key key_8812 leaked in a public repo. Give us a fresh secret for it with the same permissions.", "rotate_api_key"),
    ("Point www.acme.io at acme-shop.pages.dev with a CNAME.", "create_dns_record"),
    ("Somebody removed db_staging last week. Find out who.", "list_audit_events"),
    ("dep_9f2c is building the wrong branch. Stop it.", "cancel_deployment"),
    ("How much was key_8812 used this week?", "get_api_key_usage"),
    ("Bring back last night's backup of db_staging.", "restore_database_backup"),
    ("Add Omar (u_77) to the payments team, t_7.", "add_team_member"),
    ("inc_12 is fixed, the database is healthy again. Close it out.", "resolve_incident"),
    ("Turn new_checkout on for 10% of our users.", "set_feature_flag_rollout"),
    ("Priya's laptop (u_51) was stolen. Log her out of everything right now; she still works here.", "revoke_user_sessions"),
    ("Ship the preview build dep_7a1e to production as it is.", "promote_deployment"),
    ("db_staging keeps running out of memory. Put it on the large plan.", "resize_database"),
    ("Our endpoint was down on Tuesday and missed delivery dlv_501. Send it again.", "retry_webhook_delivery"),
    ("We need more than 20 projects. Can you get our limit raised to 50?", "request_quota_increase"),
    ("Sam (sam@acme.io) is joining finance. Get them into the org with the billing role.", "invite_user"),
    ("Is acme.io verified yet? We added the TXT record an hour ago.", "verify_domain"),
    ("ar_5 keeps paging everyone during the migration tonight. Shut it up for two hours.", "mute_alert_rule"),
    ("Set CHECKOUT_REGION to eu-west-1 on env_prod.", "set_environment_variable"),
]
