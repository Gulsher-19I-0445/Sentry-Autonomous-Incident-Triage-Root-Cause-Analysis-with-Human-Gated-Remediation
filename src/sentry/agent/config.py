import os


class Config:
    REGION = os.environ.get("AWS_REGION", "us-east-1")

    # --- Bedrock -------------------------------------------------------------
    # NOTE: bare model ids do NOT work for current Claude models — an inference
    # profile id (the "us." prefixed form) is required.
    AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-5")
    EVAL_MODEL_ID = os.environ.get("EVAL_MODEL_ID",
                                   "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    # Caps the model's OUTPUT per turn. Hitting it truncates the RCA mid-JSON,
    # which cannot be repaired and escalates the incident — an expensive way to
    # lose an investigation that had already done all its work. 4096 was enough
    # until noisy evidence started producing longer evidence lists.
    MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
    TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))

    # $ per 1M tokens, for the cost-per-incident metric. Update if rates change.
    # Cache reads bill at ~0.1x input and writes at ~1.25x (5-minute TTL), so a
    # run that caches has to price the three separately or cost_usd overstates
    # by several times.
    PRICING = {
        "sonnet": {"input": 3.00, "output": 15.00},
        "haiku": {"input": 1.00, "output": 5.00},
    }
    CACHE_READ_MULTIPLIER = 0.1
    CACHE_WRITE_MULTIPLIER = 1.25

    # Every turn resends the whole transcript, so the prefix is re-billed on each
    # one. Cache points make that a read instead. Off by default: the request
    # shape is validated server-side, so a bad one fails the whole investigation
    # rather than degrading — turn it on deliberately and check
    # cache_read_tokens on the first run.
    ENABLE_PROMPT_CACHE = os.environ.get("ENABLE_PROMPT_CACHE", "false").lower() == "true"

    # --- agent behaviour -----------------------------------------------------
    MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "8"))
    LOG_WINDOW_MINUTES = int(os.environ.get("LOG_WINDOW_MINUTES", "5"))
    LOG_QUERY_LIMIT = int(os.environ.get("LOG_QUERY_LIMIT", "15"))

    # --- scope guards --------------------------------------------------------
    # The agent may query ONLY these log groups. In a shared account this is
    # what stops it reading other teams' Glue/Amplify noise as evidence.
    TARGET_LOG_GROUPS = [
        g.strip() for g in os.environ.get(
            "TARGET_LOG_GROUPS",
            "/aws/lambda/sentry-capstone-api-gulsher,"
            "/aws/lambda/sentry-capstone-consumer-gulsher",
        ).split(",") if g.strip()
    ]
    TARGET_FUNCTIONS = [
        f.strip() for f in os.environ.get(
            "TARGET_FUNCTIONS",
            "sentry-capstone-api-gulsher,sentry-capstone-consumer-gulsher",
        ).split(",") if f.strip()
    ]

    # Sentry's own plumbing. These share the sentry-capstone- prefix with the
    # target app but are downstream of it, so a deploy here can never explain a
    # target-app failure — it is the same feedback loop the log-group allow-list
    # prevents, arriving through CloudTrail instead. Filtering them out also
    # removes the deploy churn that a sweep generates about itself.
    # COMPONENT names, not full resource names. Full names were matched as
    # substrings, which silently failed the moment a resource did not follow
    # the exact <project>-<component>-<owner> shape: the IAM role is
    # "sentry-capstone-executor-role-gulsher", with "-role-" in the MIDDLE, so
    # "sentry-capstone-executor-gulsher" is not a substring of it. The agent
    # duly reported that role's policy change as a candidate cause for a
    # target-app failure.
    #
    # Matching a hyphen-delimited token instead survives any arrangement:
    # -role- suffixes, -dlq-, log group paths, ARNs.
    OWN_COMPONENTS = [
        c.strip() for c in os.environ.get(
            "OWN_COMPONENTS",
            "agent,ingest,executor,approval,work,incidents",
        ).split(",") if c.strip()
    ]

    # --- storage -------------------------------------------------------------
    INCIDENTS_TABLE = os.environ.get("INCIDENTS_TABLE", "")
    EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")

    @classmethod
    def price_for(cls, model_id: str) -> dict:
        key = "haiku" if "haiku" in model_id.lower() else "sonnet"
        return cls.PRICING[key]