# ABOUTME: The typed registry of every setting the district manages in the app,
# ABOUTME: with its default, bounds, category, dependencies, and consequence.
"""What an Application Admin may change, and what changing it costs.

Configuration that a district can legitimately change belongs in the
application, not in an operator-held ``.env.app``: the person accountable for
the choice is a named Application Admin with a reason on the record, not
whoever last had shell access to the host.

Phase A moved the on/off switches. Phase B moves the rest of what a district
tunes -- the limits, rates, timeouts, domains, and the advisory model -- so
the whole set is edited, bounded, and historied in one place. Phase C moves
the Google identities, sign-in, and operations settings; a credential among
them is a write-only ``secret``, encrypted at rest and never read back.

Every entry carries:

``type``
    One of :mod:`apps.configuration.values`' types. It decides how a value is
    parsed from the store, from a save, and from the one-time environment
    import, and it is the only place a bound is written down.
``default``
    The value a read falls back to when the store cannot be read. It is the
    *safe* value, never the convenient one -- a switch whose failure mode
    reaches a requester defaults off, and a limit defaults to what the
    shipped configuration has always used.
``consequence``
    One sentence in the district's own terms about what a requester or a
    staff member gets, or stops getting. It is shown verbatim in the reason
    dialog; it is the only thing standing between an admin and a statutory
    mistake.
``requires``
    Other switch keys that must already be on before this one may be turned
    on. The dependency used to be a startup ``ImproperlyConfigured``; now it
    is checked on the save path, so the admin is told which other switch is
    needed instead of discovering it when the container refuses to boot.

    It is checked on enable only. Turning a parent switch *off* is never
    blocked by a dependent, because several of these are kill switches and a
    kill switch that argues with you is not one. The dependent simply goes
    inert: every consumer already requires both its own switch and its
    parent, so nothing reaches a requester through a disabled parent.

    The same asymmetry governs a switch's *prerequisite* values. Blanking the
    advisory model while advisory AI is on is allowed, and advisory AI goes
    inert; it is refused only when someone tries to turn the switch on
    without one.
``prerequisite_rule``
    What must be true before the switch may be turned on: the name of a
    function in :mod:`apps.configuration.prerequisites`, which reads the
    settings store (and, for what is still environment-managed, Django
    settings) and returns the sentence the admin sees.
``relations``
    Ordering constraints between two settings (a combined ceiling that may
    not fall below its per-item ceiling, a staleness window that may not be
    shorter than the recheck it bounds). Declared once in :data:`RELATIONS`
    and checked on the save path from either side.
"""

from dataclasses import dataclass

from . import values


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str
    default: object
    category: str
    label: str
    consequence: str
    requires: tuple[str, ...] = ()
    prerequisite_rule: str = ""
    #: Inclusive bounds for ``integer``.
    minimum: int = 0
    maximum: int = 0
    #: ``text``/``email``/``domain``/``secret`` shape.
    max_length: int = 255
    #: The fixed set a ``choice`` may take, lower-case.
    choices: tuple[str, ...] = ()
    pattern: str = ""
    format_message: str = ""
    allow_blank: bool = False
    #: Ceiling on ``domain_list``/``email_list`` membership.
    max_items: int = 50
    #: Periods a ``rate`` may use; empty means every period DRF understands.
    periods: tuple[str, ...] = ()
    #: What the number counts, for the Settings page to render beside it.
    unit: str = ""
    #: Variables this key used to take its default from before it moved into
    #: the store. Consulted only by the one-time bootstrap, and only when the
    #: key's own environment variable is unset.
    bootstrap_from: tuple[str, ...] = ()

    def __post_init__(self):
        if self.type not in values.TYPES:
            raise ValueError(f"{self.key} declares unknown type {self.type!r}.")
        if self.type == values.CHOICE and not self.choices:
            raise ValueError(f"{self.key} is a choice with no choices.")
        if self.type != values.CHOICE and self.choices:
            raise ValueError(f"{self.key} lists choices but is not a choice.")
        if self.type == values.SECRET and self.default != "":
            # A shipped default for a secret would be a credential in source.
            raise ValueError(f"{self.key} is a secret and may not carry a default.")


@dataclass(frozen=True)
class Relation:
    """``greater`` may never be less than ``lesser``."""

    greater: str
    lesser: str
    message: str


CATEGORY_CORRESPONDENCE = "Requester correspondence"
CATEGORY_GMAIL = "Gmail transport"
CATEGORY_COMPLIANCE = "Statutory compliance"
CATEGORY_ADVISORY_AI = "Advisory AI"
CATEGORY_STAFF = "Staff notifications"
CATEGORY_OBSERVABILITY = "Observability"
CATEGORY_INTAKE = "Public intake"
CATEGORY_DOMAINS = "Domains and people"
CATEGORY_RECORDS = "Records handling"
CATEGORY_ACCESS = "Access and tokens"
CATEGORY_GOOGLE_IDENTITY = "Google identities"
CATEGORY_SIGN_IN = "Staff sign-in"
CATEGORY_OPERATIONS = "Operations"
CATEGORY_INTEGRATIONS = "Integrations"

_MIB = 1024 * 1024
_GIB = 1024**3

#: Rates on staff and requester endpoints. ``day`` is deliberately absent:
#: a daily bucket on a login or an access-code attempt would let an entire
#: day's allowance be spent in one second.
_SHORT_PERIODS = ("s", "sec", "m", "min", "h", "hour")


SETTINGS: tuple[SettingSpec, ...] = (
    # ---------------------------------------------------------------- switches
    SettingSpec(
        key="PRR_CORRESPONDENCE_OUTBOUND_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_CORRESPONDENCE,
        label="Send requester correspondence",
        consequence=(
            "On, approved requester correspondence leaves the building. Off is "
            "the outbound kill switch: messages still draft, approve, and "
            "queue, but nothing is delivered to a requester and every "
            "statutory clock keeps running."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_CORRESPONDENCE,
        label="Allow automated correspondence to send",
        consequence=(
            "On, the two permitted automated messages (the day-5 "
            "acknowledgment safety net and the non-statutory submission "
            "receipt) may deliver. Off, only a human-approved message sends."
        ),
        requires=("PRR_CORRESPONDENCE_OUTBOUND_ENABLED",),
    ),
    SettingSpec(
        key="AUTO_ACK_SAFETY_NET",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_COMPLIANCE,
        label="Day-5 acknowledgment safety net",
        consequence=(
            "On, a request with no human response by its last RCW 42.56.520 "
            "business day gets the acknowledgment queued automatically. Off, "
            "there is no automatic day-5 acknowledgment: if a human misses "
            "the deadline the acknowledgment is late, and a late "
            "acknowledgment is strict liability under RCW 42.56.520."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_GMAIL,
        label="Gmail transport",
        consequence=(
            "On, the application may reach the pinned district mailbox to "
            "send and read mail. Off, no requester message and no staff alert "
            "leaves the application by any route."
        ),
        prerequisite_rule="gmail_transport_configured",
    ),
    SettingSpec(
        key="PRR_GMAIL_INBOUND_POLL_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_GMAIL,
        label="Poll the mailbox for inbound mail",
        consequence=(
            "On, inbound requester mail is pulled in, matched to a case, and "
            "put in the Inbox. Off, mail sits unread in the mailbox and "
            "nothing new arrives in the work queue."
        ),
        requires=("PRR_GMAIL_ENABLED",),
    ),
    SettingSpec(
        key="PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_GMAIL,
        label="Archive mail once it is received",
        consequence=(
            "Mailbox hygiene only, never a processing gate. On, a message "
            "that has already been durably received also leaves the mailbox "
            "INBOX. Off, it stays in the INBOX with its processed label."
        ),
        requires=("PRR_GMAIL_ENABLED",),
    ),
    SettingSpec(
        key="PRR_STAFF_NOTIFICATIONS_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_STAFF,
        label="Send staff alerts",
        consequence=(
            "On, preservation and operations alerts reach district staff. "
            "Off, staff learn about a deadline or a failure only by opening "
            "the application. No requester is affected either way."
        ),
        requires=("PRR_GMAIL_ENABLED",),
        prerequisite_rule="staff_notification_recipients_configured",
    ),
    SettingSpec(
        key="PRR_AI_ADVISORY_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Advisory AI",
        consequence=(
            "On, staff may ask the approved model for a proposal. A proposal "
            "is never applied: a named human still decides every "
            "responsiveness, exemption, redaction, and closure outcome. Off, "
            "no request or record text reaches the model at all."
        ),
        prerequisite_rule="advisory_model_configured",
    ),
    SettingSpec(
        key="PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Prepare record proposals automatically",
        consequence=(
            "On, a record's advisory proposal is prepared as the record "
            "arrives instead of when a reviewer clicks Run. Off, a reviewer "
            "asks for each one. Nothing is applied either way."
        ),
        requires=("PRR_AI_ADVISORY_ENABLED",),
    ),
    SettingSpec(
        key="PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Prepare request-intent proposals automatically",
        consequence=(
            "On, a new request gets its intent proposal at intake, and a "
            "completed proposal mapped to an active Vault-backed system runs "
            "one unattended search count. The count reaches no requester, "
            "holds nothing, and exports nothing. Off, an officer asks for "
            "each proposal."
        ),
        requires=("PRR_AI_ADVISORY_ENABLED",),
    ),
    SettingSpec(
        key="PRR_TRUST_RESPONSIVE_MODE",
        type=values.CHOICE,
        default="suggest",
        choices=("suggest", "batch"),
        category=CATEGORY_ADVISORY_AI,
        label="Trust ladder: responsive",
        consequence=(
            "Suggest: a reviewer decides each record. Batch: a reviewer may "
            "approve a whole cluster as responsive after judging a random "
            "sample; one disagreement blocks the batch. There is no automatic "
            "mode for responsive: a responsive decision always stays with a "
            "person."
        ),
    ),
    SettingSpec(
        key="PRR_TRUST_NON_RESPONSIVE_MODE",
        type=values.CHOICE,
        default="suggest",
        choices=("suggest", "batch", "auto"),
        category=CATEGORY_ADVISORY_AI,
        label="Trust ladder: non-responsive",
        consequence=(
            "Suggest: a reviewer decides each record. Batch: a reviewer may "
            "approve a cluster as non-responsive after a sampled check. Auto: "
            "records the AI calls non-responsive at or above the confidence "
            "below are staged as Ready to discard, and nothing is applied "
            "until a reviewer finds no responsive record in a random elusion "
            "sample of that pile and signs off."
        ),
    ),
    SettingSpec(
        key="PRR_TRUST_REDACTION_MODE",
        type=values.CHOICE,
        default="suggest",
        choices=("suggest", "batch"),
        category=CATEGORY_ADVISORY_AI,
        label="Trust ladder: redaction proposals",
        consequence=(
            "Suggest: a reviewer accepts or rejects each proposed redaction. "
            "Batch: a reviewer may accept every pending proposal with one "
            "exemption and pattern on a case after accepting a random sample "
            "of them; one rejection blocks the group. Each accepted proposal "
            "is still its own redaction with its exemption citation, and "
            "second review still applies where it is required. There is no "
            "automatic mode."
        ),
    ),
    SettingSpec(
        key="PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE",
        type=values.INTEGER,
        default=95,
        minimum=50,
        maximum=100,
        unit="percent",
        category=CATEGORY_ADVISORY_AI,
        label="Ready-to-discard confidence floor",
        consequence=(
            "In auto mode, only records the AI calls non-responsive with at "
            "least this confidence are staged for discard."
        ),
    ),
    SettingSpec(
        key="PRR_TRUST_BATCH_SAMPLE_SIZE",
        type=values.INTEGER,
        default=10,
        minimum=1,
        maximum=200,
        unit="records",
        category=CATEGORY_ADVISORY_AI,
        label="Cluster approval sample size",
        consequence=(
            "How many records, chosen at random, a reviewer judges before a "
            "cluster can be approved. A smaller cluster is judged in full."
        ),
    ),
    SettingSpec(
        key="PRR_TRUST_ELUSION_SAMPLE_SIZE",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=500,
        unit="records",
        category=CATEGORY_ADVISORY_AI,
        label="Elusion sample size",
        consequence=(
            "How many records, chosen at random from the Ready to discard "
            "pile, a reviewer checks for missed responsive records. Larger "
            "samples catch rarer misses: with no finding in 30 records the "
            "pile's miss rate is below about 10% at 95% confidence."
        ),
    ),
    SettingSpec(
        key="PRR_INTENT_MAX_COUNT_ITERATIONS",
        type=values.INTEGER,
        default=4,
        minimum=1,
        maximum=10,
        unit="counts",
        category=CATEGORY_ADVISORY_AI,
        label="Scoping counts per request",
        consequence=(
            "How many Google Vault counts iterative scoping may run for one "
            "request: the intent proposal's count, then each tightened query "
            "the agent proposes after reading the total and the custodian "
            "breakdown. Counts only -- nothing is held or exported. 1 turns "
            "refinement off and keeps the single proposal count."
        ),
    ),
    SettingSpec(
        key="PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Draft interpretation-and-estimate letters automatically",
        consequence=(
            "On, once a public records case has a completed intent proposal "
            "and every unreviewed record has an AI suggestion, the system "
            "computes an estimate and drafts the interpretation-and-estimate "
            "letter for a Records Officer. The draft is never approved or "
            "sent by the system; an officer approves and sends it. Off, no "
            "letter is drafted automatically."
        ),
        requires=("PRR_AI_ADVISORY_ENABLED",),
    ),
    SettingSpec(
        key="PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Name new cases automatically",
        consequence=(
            "On, each new case gets a one-line title proposed from its own "
            "request text, so staff lists show what was asked for instead of "
            "the first line of an email. The title is a label on a screen: it "
            "reaches no requester, changes no status, runs no search, and "
            "never edits the request. Off, lists show the opening of the "
            "request wording instead."
        ),
        requires=("PRR_AI_ADVISORY_ENABLED",),
    ),
    SettingSpec(
        key="PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_ADVISORY_AI,
        label="Classify new cases automatically",
        consequence=(
            "On, each new case gets a suggested label -- new request, "
            "follow-up to or duplicate of an earlier case, related to one, "
            "not a records request, or spam -- for a Records Officer to "
            "confirm or dismiss. The label merges, closes, and sends nothing; "
            "the officer applies it through the ordinary action. Off, new "
            "cases are triaged by hand as before."
        ),
        requires=("PRR_AI_ADVISORY_ENABLED",),
    ),
    SettingSpec(
        key="PRR_CLOUDWATCH_METRICS_ENABLED",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_OBSERVABILITY,
        label="Publish CloudWatch metrics",
        consequence=(
            "On, inbound-poll health is published to CloudWatch so an alarm "
            "can fire when polling stalls. Off, the same health is visible "
            "only inside the application. No requester is affected."
        ),
        prerequisite_rule="cloudwatch_region_configured",
    ),
    # -------------------------------------------------------------- advisory AI
    SettingSpec(
        key="PRR_AI_PROVIDER",
        type=values.TEXT,
        default="bedrock",
        max_length=7,
        pattern=r"(?i:bedrock)",
        format_message="Advisory AI is approved only for the Bedrock provider.",
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model provider",
        consequence=(
            "Which service the district has approved to see request and "
            "record text. Amazon Bedrock is the only approved provider: it "
            "runs in the district's own AWS account and does not retain the "
            "text. Advisory AI cannot be enabled with any other value."
        ),
    ),
    SettingSpec(
        key="PRR_AI_AWS_REGION",
        bootstrap_from=("AWS_DEFAULT_REGION",),
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=64,
        pattern=r"[a-z]{2}(?:-gov)?-[a-z]+-\d",
        format_message=(
            "The advisory model region must be an AWS region such as "
            "us-west-2."
        ),
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model region",
        consequence=(
            "The AWS region the advisory model is called in, which is where "
            "request and record text is processed. Leave it blank and "
            "advisory AI cannot be enabled."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model",
        consequence=(
            "The exact Bedrock model identifier every advisory proposal is "
            "asked of, and the one recorded on each run. Changing it changes "
            "what future proposals come from; recorded runs keep the model "
            "they actually used."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL_INTENT",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Request-intent model",
        consequence=(
            "The Bedrock model that reads a request and proposes its scope. Blank uses the advisory model above. Only models the "
            "production Bedrock grant names can be called; any other "
            "identifier makes these runs fail closed."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL_CASE_TITLE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Case-title model",
        consequence=(
            "The Bedrock model that proposes a case's one-line title. Blank uses the advisory model above. Only models the "
            "production Bedrock grant names can be called; any other "
            "identifier makes these runs fail closed."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL_RESPONSIVENESS",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Record-responsiveness model",
        consequence=(
            "The Bedrock model that suggests whether a record is responsive. Blank uses the advisory model above. Only models the "
            "production Bedrock grant names can be called; any other "
            "identifier makes these runs fail closed."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL_INTAKE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Intake-classification model",
        consequence=(
            "The Bedrock model that suggests whether a new case is a new "
            "request, a follow-up, a duplicate, related, a non-request, or "
            "spam. Blank uses the advisory model above. Only models the "
            "production Bedrock grant names can be called; any other "
            "identifier makes these runs fail closed."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MODEL_REDACTION",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        format_message="The advisory model must be a Bedrock model identifier.",
        category=CATEGORY_ADVISORY_AI,
        label="Redaction-candidate model",
        consequence=(
            "The Bedrock model that proposes redaction candidates. Blank uses the advisory model above. Only models the "
            "production Bedrock grant names can be called; any other "
            "identifier makes these runs fail closed."
        ),
    ),
    SettingSpec(
        key="PRR_AI_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=120,
        unit="seconds",
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model timeout",
        consequence=(
            "How long one advisory call may take before it is abandoned and "
            "the run is marked failed. Staff see a failed proposal, never a "
            "wrong one."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MAX_INPUT_CHARACTERS",
        type=values.INTEGER,
        default=50000,
        minimum=1000,
        maximum=250000,
        unit="characters",
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model input ceiling",
        consequence=(
            "The most request and record text one advisory call may send. "
            "Longer material is truncated before it leaves, so raising this "
            "sends more district text to the model and lowering it sends "
            "less."
        ),
    ),
    SettingSpec(
        key="PRR_AI_MAX_RESPONSE_BYTES",
        type=values.INTEGER,
        default=64 * 1024,
        minimum=1024,
        maximum=_MIB,
        unit="bytes",
        category=CATEGORY_ADVISORY_AI,
        label="Advisory model response ceiling",
        consequence=(
            "The largest advisory response the application will parse. A "
            "bigger reply is rejected as a failed run rather than stored."
        ),
    ),
    # ---------------------------------------------------------- correspondence
    SettingSpec(
        key="PRR_CORRESPONDENCE_REPLY_LOCAL_PART",
        type=values.TEXT,
        default="publicrecords",
        max_length=27,
        pattern=r"(?!\.)(?!.*\.\.)[A-Za-z0-9._-]+(?<!\.)",
        format_message=(
            "The reply local part must be letters, numbers, dots, "
            "underscores, or hyphens, without leading, trailing, or "
            "consecutive dots."
        ),
        category=CATEGORY_CORRESPONDENCE,
        label="Reply address local part",
        consequence=(
            "The part before the @ in the tokenised address every requester "
            "reply is sent to. Changing it means replies to messages already "
            "sent no longer match a case automatically and land as unmatched "
            "mail."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_REPLY_DOMAIN",
        type=values.DOMAIN,
        default="psd401.net",
        category=CATEGORY_CORRESPONDENCE,
        label="Reply address domain",
        consequence=(
            "The domain requesters reply to. It must be a domain the "
            "district's mail actually accepts; a wrong one silently strands "
            "every requester reply."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_MESSAGE_ID_DOMAIN",
        type=values.DOMAIN,
        default="prr.psd401.net",
        category=CATEGORY_CORRESPONDENCE,
        label="Message-ID domain",
        consequence=(
            "The domain stamped into the Message-ID of each message the "
            "application sends, which is how a reply is threaded back to its "
            "case. Changing it affects only messages sent afterwards."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_LEASE_SECONDS",
        type=values.INTEGER,
        default=300,
        minimum=30,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_CORRESPONDENCE,
        label="Delivery lease",
        consequence=(
            "How long one worker holds a queued message while it sends. Too "
            "short and a slow send can be picked up twice; too long and a "
            "crashed worker's message waits that long for another to take it."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_STUCK_SECONDS",
        type=values.INTEGER,
        default=3600,
        minimum=300,
        maximum=86400,
        unit="seconds",
        category=CATEGORY_CORRESPONDENCE,
        label="Stuck-message alert threshold",
        consequence=(
            "How long a queued message may sit undelivered before it is "
            "reported as stuck. It changes when staff are told, never "
            "whether the message sends."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_ATTACHMENT_SCAN_STALE_SECONDS",
        type=values.INTEGER,
        default=300,
        minimum=30,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_CORRESPONDENCE,
        label="Attachment scan stale window",
        consequence=(
            "How long an attachment may sit in malware scanning before the "
            "scan is treated as abandoned and retried. No unscanned file is "
            "ever attached either way."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_RESERVATION_STALE_SECONDS",
        type=values.INTEGER,
        default=900,
        minimum=300,
        maximum=86400,
        unit="seconds",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound reservation stale window",
        consequence=(
            "How long a half-finished inbound message stays reserved before "
            "another worker may take it. Shorter risks two workers on one "
            "message; longer delays a retry after a worker dies."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT",
        bootstrap_from=("PRR_INTAKE_ATTACHMENT_MAX_COUNT",),
        type=values.INTEGER,
        default=10,
        minimum=1,
        maximum=10,
        unit="files",
        category=CATEGORY_CORRESPONDENCE,
        label="Attachments per message",
        consequence=(
            "The most files staff may attach to one requester message. "
            "Larger productions go out as installment downloads, not as mail."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES",
        bootstrap_from=("PRR_INTAKE_ATTACHMENT_MAX_BYTES",),
        type=values.INTEGER,
        default=25 * _MIB,
        minimum=1,
        maximum=25 * _MIB,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Attachment size limit",
        consequence=(
            "The largest single file that may be attached to a requester "
            "message. Gmail rejects larger mail outright, so a bigger value "
            "would only turn a clear refusal into a failed send."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES",
        bootstrap_from=("PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES",),
        type=values.INTEGER,
        default=50 * _MIB,
        minimum=1,
        maximum=50 * _MIB,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Attachment combined size limit",
        consequence=(
            "The combined size of every file on one requester message."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES",
        type=values.INTEGER,
        default=75 * _MIB,
        minimum=1,
        maximum=75 * _MIB,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound message size limit",
        consequence=(
            "The largest inbound message the application will take in. A "
            "larger one is refused and reported rather than truncated, so "
            "nothing silently loses part of what a requester sent."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_METADATA_MAX_BYTES",
        type=values.INTEGER,
        default=16 * 1024,
        minimum=2,
        maximum=64 * 1024,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound metadata size limit",
        consequence=(
            "The most header metadata kept per inbound message. It bounds "
            "the stored evidence, not whether the message is received."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES",
        type=values.INTEGER,
        default=_MIB,
        minimum=1,
        maximum=4 * _MIB,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound body size limit",
        consequence=(
            "How much of an inbound message body is read. A body beyond this "
            "is refused rather than cut short."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS",
        type=values.INTEGER,
        default=200,
        minimum=1,
        maximum=500,
        unit="headers",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound header count limit",
        consequence=(
            "A ceiling on headers per inbound message, so a crafted message "
            "cannot exhaust a worker parsing it."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES",
        type=values.INTEGER,
        default=998,
        minimum=78,
        maximum=998,
        unit="bytes",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound header line limit",
        consequence=(
            "The longest header line accepted. 998 is the limit RFC 5322 "
            "sets; below it, legitimate long subject lines start being "
            "refused."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS",
        type=values.INTEGER,
        default=100,
        minimum=1,
        maximum=500,
        unit="parts",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound MIME part limit",
        consequence=(
            "A ceiling on the parts of one inbound message, so a crafted "
            "message cannot exhaust a worker parsing it."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH",
        type=values.INTEGER,
        default=20,
        minimum=1,
        maximum=30,
        unit="levels",
        category=CATEGORY_CORRESPONDENCE,
        label="Inbound MIME nesting limit",
        consequence=(
            "How deeply an inbound message may nest attachments inside "
            "attachments before it is refused."
        ),
    ),
    SettingSpec(
        key="PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST",
        type=values.EMAIL_LIST,
        default=(),
        allow_blank=True,
        max_items=25,
        category=CATEGORY_CORRESPONDENCE,
        label="Non-production recipient allowlist",
        consequence=(
            "In a non-production environment, the only addresses mail may go "
            "to. It has no effect in production, where every approved "
            "recipient is reachable; leaving it empty in a test environment "
            "means no mail is delivered at all."
        ),
    ),
    # ----------------------------------------------------------------- Gmail
    SettingSpec(
        key="PRR_GMAIL_MAILBOX",
        type=values.EMAIL,
        default="publicrecords@psd401.net",
        # 100, not the 254 an address may be: the mailbox is written into
        # every correspondence row's provider_account column, and that
        # boundary is what api.E017 used to guard at startup.
        max_length=100,
        category=CATEGORY_GMAIL,
        label="District mailbox",
        consequence=(
            "The one mailbox the application sends from and reads. Changing "
            "it points the whole transport at a different mailbox: mail "
            "already waiting in the old one is never polled again."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=120,
        unit="seconds",
        category=CATEGORY_GMAIL,
        label="Gmail request timeout",
        consequence=(
            "How long one Gmail API call may take before it is abandoned and "
            "retried. A send that times out is retried, never dropped."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_INBOUND_LOOKBACK_DAYS",
        type=values.INTEGER,
        default=7,
        minimum=1,
        maximum=30,
        unit="days",
        category=CATEGORY_GMAIL,
        label="Inbound poll lookback",
        consequence=(
            "How far back each poll looks for unprocessed mail. Shortening "
            "it below the time polling has been stopped means older "
            "requester mail is never picked up."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_INBOUND_PROCESSED_LABEL",
        type=values.TEXT,
        default="PRR/Processed",
        max_length=225,
        pattern=r"[A-Za-z0-9_./-]+",
        format_message=(
            "The processed label may use letters, numbers, dots, slashes, "
            "underscores, and hyphens."
        ),
        category=CATEGORY_GMAIL,
        label="Processed label",
        consequence=(
            "The Gmail label put on a message once it is durably received. "
            "Changing it makes every already-labelled message look "
            "unprocessed to the next poll, which the per-message duplicate "
            "check still catches."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_INBOUND_STALE_SECONDS",
        type=values.INTEGER,
        default=300,
        minimum=120,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_GMAIL,
        label="Inbound poll stale threshold",
        consequence=(
            "How long inbound polling may go without a successful run before "
            "it is reported as stalled. It changes when staff and the "
            "CloudWatch alarm are told, never whether polling runs."
        ),
    ),
    # --------------------------------------------------------- public intake
    SettingSpec(
        key="PRR_PUBLIC_INTAKE_PER_IP_RATE",
        type=values.RATE,
        default="10/hour",
        category=CATEGORY_INTAKE,
        label="Public form rate per address",
        consequence=(
            "How many requests one internet address may submit through the "
            "public form. Lowering it can stop a member of the public filing "
            "a legitimate second request; the PRA does not let the district "
            "limit how many requests a person may make, so keep it generous."
        ),
    ),
    SettingSpec(
        key="PRR_PUBLIC_INTAKE_GLOBAL_RATE",
        type=values.RATE,
        default="500/day",
        category=CATEGORY_INTAKE,
        label="Public form rate overall",
        consequence=(
            "The ceiling on public-form submissions from everyone combined. "
            "It is the last brake before an automated flood; once it is hit, "
            "real requesters are turned away too."
        ),
    ),
    SettingSpec(
        key="PRR_PUBLIC_INTAKE_MIN_FILL_SECONDS",
        type=values.INTEGER,
        default=3,
        minimum=0,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_INTAKE,
        label="Minimum form fill time",
        consequence=(
            "A form returned faster than this is treated as automated. Set "
            "it high and a requester who pastes a prepared request is "
            "refused; set it to zero and the check is off."
        ),
    ),
    SettingSpec(
        key="PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS",
        type=values.INTEGER,
        default=86400,
        minimum=60,
        maximum=604800,
        unit="seconds",
        category=CATEGORY_INTAKE,
        label="Public form token lifetime",
        consequence=(
            "How long an opened public form stays submittable. A requester "
            "who leaves the form open longer has to reload it, and anything "
            "typed is lost."
        ),
    ),
    SettingSpec(
        key="PRR_PUBLIC_INTAKE_PROCESSING_STALE_MINUTES",
        type=values.INTEGER,
        default=15,
        minimum=1,
        maximum=1440,
        unit="minutes",
        category=CATEGORY_INTAKE,
        label="Intake processing stale window",
        consequence=(
            "How long a half-processed public submission may sit before it is "
            "retried. The request number is already issued, so this changes "
            "only how quickly the case finishes being built."
        ),
    ),
    SettingSpec(
        key="PRR_INTAKE_ATTACHMENT_MAX_COUNT",
        type=values.INTEGER,
        default=10,
        minimum=1,
        maximum=100,
        unit="files",
        category=CATEGORY_INTAKE,
        label="Public form file count",
        consequence=(
            "The most files a member of the public may attach to one "
            "request. Every one is quarantined and malware-scanned before "
            "anyone may open it."
        ),
    ),
    SettingSpec(
        key="PRR_INTAKE_ATTACHMENT_MAX_BYTES",
        type=values.INTEGER,
        default=25 * _MIB,
        minimum=1,
        maximum=100 * _MIB,
        unit="bytes",
        category=CATEGORY_INTAKE,
        label="Public form file size",
        consequence=(
            "The largest single file a member of the public may attach. A "
            "requester whose file is over the limit is told so at once "
            "rather than after uploading."
        ),
    ),
    SettingSpec(
        key="PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES",
        type=values.INTEGER,
        default=50 * _MIB,
        minimum=1,
        maximum=500 * _MIB,
        unit="bytes",
        category=CATEGORY_INTAKE,
        label="Public form combined file size",
        consequence=(
            "The combined size of every file on one public request."
        ),
    ),
    SettingSpec(
        key="PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS",
        type=values.DOMAIN_LIST,
        default=("nextrequest.com",),
        allow_blank=True,
        max_items=100,
        category=CATEGORY_INTAKE,
        label="Blocked intake sender domains",
        consequence=(
            "Mail from these domains never opens a new request. It is how "
            "the retired NextRequest tenant's forwarded mail stops creating "
            "duplicate cases. Removing a domain lets its mail open cases "
            "again; adding one silently stops mail from that domain "
            "becoming a request."
        ),
    ),
    # ----------------------------------------------------- domains and people
    SettingSpec(
        key="PRR_DISTRICT_EMAIL_DOMAINS",
        bootstrap_from=("PRR_GOOGLE_HOSTED_DOMAIN",),
        type=values.DOMAIN_LIST,
        default=("psd401.net",),
        allow_blank=True,
        category=CATEGORY_DOMAINS,
        label="District mail domains",
        consequence=(
            "A record whose sender is outside these domains is tagged as "
            "having an external sender, which is a review aid and not an "
            "authorisation boundary. Narrowing it tags more records "
            "external; emptying it tags every record external."
        ),
    ),
    SettingSpec(
        key="PRR_CUSTODIAN_ACCOUNT_DOMAINS",
        bootstrap_from=("PRR_DISTRICT_EMAIL_DOMAINS",),
        type=values.DOMAIN_LIST,
        default=("psd401.net",),
        allow_blank=True,
        category=CATEGORY_DOMAINS,
        label="Custodian account domains",
        consequence=(
            "Only accounts in these domains may be recorded as the custodian "
            "of a record from a Vault export. It is deliberately separate "
            "from the district mail domains so widening a review tag can "
            "never widen what is written as custody evidence, and it must "
            "never include a student domain."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_GROUP_ALLOWED_DOMAINS",
        bootstrap_from=("PRR_GOOGLE_HOSTED_DOMAIN",),
        type=values.DOMAIN_LIST,
        default=("psd401.net",),
        category=CATEGORY_DOMAINS,
        label="Google group domains",
        consequence=(
            "The only Workspace domains whose groups may be mapped to a "
            "staff role. Removing a domain in use strips the roles everyone "
            "held through its groups at their next revalidation; direct "
            "grants are unaffected."
        ),
    ),
    SettingSpec(
        key="PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS",
        bootstrap_from=("PRA_OFFICER_EMAIL",),
        type=values.EMAIL_LIST,
        default=("publicrecords@psd401.net",),
        allow_blank=True,
        max_items=25,
        category=CATEGORY_DOMAINS,
        label="Records-officer alert addresses",
        consequence=(
            "Who receives preservation and operations alerts. Emptying it "
            "sends the alerts nowhere; staff alerts cannot be turned on "
            "without at least one address."
        ),
    ),
    SettingSpec(
        key="PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS",
        type=values.INTEGER,
        default=86400,
        minimum=60,
        maximum=604800,
        unit="seconds",
        category=CATEGORY_DOMAINS,
        label="Repeat alert suppression",
        consequence=(
            "How long the same alert is held back after being sent once, so "
            "one stuck case does not bury the officer's inbox. A longer "
            "window means a still-unresolved problem is mentioned less "
            "often."
        ),
    ),
    # ------------------------------------------------------- records handling
    SettingSpec(
        key="CAMERA_RETENTION_DAYS",
        type=values.INTEGER,
        default=10,
        minimum=1,
        maximum=3650,
        unit="days",
        category=CATEGORY_RECORDS,
        label="Camera retention window",
        consequence=(
            "How long the district's cameras are believed to keep footage. "
            "A request landing inside this window puts an immediate hold on "
            "every camera. Shortening it below the true retention means "
            "footage that still exists is never held and can be overwritten "
            "— a destroyed public record. Outside the window the request "
            "still gets a human search and an adequate-search checklist; "
            "nothing is ever denied automatically."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_PREVIEW_MAX_BYTES",
        type=values.INTEGER,
        default=500 * _MIB,
        minimum=_MIB,
        maximum=10 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Preview size limit",
        consequence=(
            "The largest record the reviewer preview will render. A bigger "
            "record is still collected, reviewed, and produced; it is opened "
            "by download instead of in the browser."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_PREVIEW_MAX_PAGES",
        type=values.INTEGER,
        default=5000,
        minimum=1,
        maximum=100000,
        unit="pages",
        category=CATEGORY_RECORDS,
        label="Preview page limit",
        consequence=(
            "The most pages of one record the preview will render."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_PREVIEW_DPI",
        type=values.INTEGER,
        default=144,
        minimum=72,
        maximum=300,
        unit="DPI",
        category=CATEGORY_RECORDS,
        label="Preview resolution",
        consequence=(
            "How finely a page is rendered for on-screen review. Too low and "
            "a reviewer can miss small print they are deciding to redact."
        ),
    ),
    SettingSpec(
        key="PRR_REDACTION_DPI",
        type=values.INTEGER,
        default=200,
        minimum=96,
        maximum=600,
        unit="DPI",
        category=CATEGORY_RECORDS,
        label="Redaction burn-in resolution",
        consequence=(
            "The resolution a redacted page is rasterised at for release. "
            "The text under a redaction is destroyed at any value; a low one "
            "only makes the released page harder for the requester to read."
        ),
    ),
    SettingSpec(
        key="PRR_DERIVATIVE_MAX_SOURCE_BYTES",
        type=values.INTEGER,
        default=200 * _MIB,
        minimum=1024,
        maximum=2 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Derivative source size limit",
        consequence=(
            "The largest record handed to a converter to make a reviewable "
            "rendition. Conversion holds the whole file in memory, so this "
            "is deliberately well under the preview ceiling."
        ),
    ),
    SettingSpec(
        key="PRR_OFFICE_CONVERTER_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=180,
        minimum=10,
        maximum=1800,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Office converter timeout",
        consequence=(
            "How long one office-document conversion may run before it is "
            "killed. These documents arrive from outside the district and a "
            "malformed one can hang the converter indefinitely."
        ),
    ),
    SettingSpec(
        key="PRR_MEDIA_STREAM_MAX_BYTES",
        type=values.INTEGER,
        default=_GIB,
        minimum=1024,
        maximum=8 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Media streaming size limit",
        consequence=(
            "The largest recording streamed for review. Streaming holds the "
            "whole recording in memory to verify its digest before a single "
            "byte is served, so this stays well under the storage limit."
        ),
    ),
    SettingSpec(
        key="PRR_MEDIA_TOOL_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=60,
        minimum=5,
        maximum=600,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Media inspection timeout",
        consequence=(
            "How long the application may spend inspecting a recording's "
            "duration and streams before giving up."
        ),
    ),
    SettingSpec(
        key="PRR_MEDIA_RENDER_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=3600,
        minimum=60,
        maximum=21600,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Media redaction render timeout",
        consequence=(
            "How long re-encoding a redacted recording may run. The only "
            "faster alternative is copying the stream, which would leave the "
            "unredacted frames in the released file, so a timeout fails the "
            "render rather than releasing it."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_UPLOAD_MAX_COUNT",
        type=values.INTEGER,
        default=20,
        minimum=1,
        maximum=50,
        unit="files",
        category=CATEGORY_RECORDS,
        label="Staff record upload file count",
        consequence=(
            "The most files a Records Officer may add to a case in one "
            "upload. Each is malware-scanned before the upload answers, so a "
            "larger batch is a longer wait; more files go in another upload."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_UPLOAD_MAX_BYTES",
        type=values.INTEGER,
        default=100 * _MIB,
        minimum=1,
        maximum=500 * _MIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Staff record upload file size",
        consequence=(
            "The largest single file a Records Officer may upload to a case. "
            "A file the malware scanner will not accept whole is refused, "
            "never stored unscanned, so raising this past the scanner's own "
            "stream limit only turns a size refusal into a scan refusal."
        ),
    ),
    SettingSpec(
        key="PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES",
        type=values.INTEGER,
        default=250 * _MIB,
        minimum=1,
        maximum=2 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Staff record upload combined size",
        consequence=(
            "The combined size of every file in one staff record upload. "
            "The whole upload is scanned and stored while the officer waits."
        ),
    ),
    SettingSpec(
        key="PRR_LEGACY_FILE_MAX_BYTES",
        type=values.INTEGER,
        default=500 * _MIB,
        minimum=1024,
        maximum=8 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Migrated legacy file size limit",
        consequence=(
            "The largest migrated legacy file streamed through malware "
            "scanning. Legacy collections routinely exceed what the public "
            "form allows, so they have their own ceiling."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_EXPORT_MAX_BYTES",
        type=values.INTEGER,
        default=10 * _GIB,
        minimum=_MIB,
        maximum=100 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Vault export object size limit",
        consequence=(
            "The largest single object taken from a Vault export. An export "
            "with a bigger object stops with a reported error rather than "
            "ingesting part of it."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_EXPORT_TOTAL_MAX_BYTES",
        type=values.INTEGER,
        default=50 * _GIB,
        minimum=_MIB,
        maximum=500 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Vault export combined size limit",
        consequence=(
            "The combined size of one Vault export's objects."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_ARCHIVE_MAX_MEMBERS",
        type=values.INTEGER,
        default=10000,
        minimum=1,
        maximum=100000,
        unit="files",
        category=CATEGORY_RECORDS,
        label="Archive member limit",
        consequence=(
            "How many files one archive from an export may contain before it "
            "is refused as a decompression bomb."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES",
        type=values.INTEGER,
        default=100 * _GIB,
        minimum=_MIB,
        maximum=500 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Archive expanded size limit",
        consequence=(
            "How large one archive may expand to before it is refused as a "
            "decompression bomb."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_MESSAGE_MAX_BYTES",
        type=values.INTEGER,
        default=100 * _MIB,
        minimum=1024,
        maximum=2 * _GIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Vault message size limit",
        consequence=(
            "The largest single message parsed out of a Vault export."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_OCR_TEXT_MAX_CHARS",
        type=values.INTEGER,
        default=5000000,
        minimum=10000,
        maximum=50000000,
        unit="characters",
        category=CATEGORY_RECORDS,
        label="OCR text limit",
        consequence=(
            "How much recognised text is kept per record for search. Text "
            "beyond it is not indexed, so a reviewer searching for a term "
            "deep in a very long scan may not find it."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_INGEST_LEASE_SECONDS",
        type=values.INTEGER,
        default=3600,
        minimum=60,
        maximum=86400,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Vault ingest lease",
        consequence=(
            "How long one worker holds an export while ingesting it. Too "
            "short and a slow ingest can be picked up twice; too long and a "
            "crashed worker's export waits that long to resume."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS",
        type=values.INTEGER,
        default=900,
        minimum=60,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Vault ingest enqueue lease",
        consequence=(
            "How long an export stays claimed between being found ready and "
            "a worker starting on it."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_GCS_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=120,
        minimum=5,
        maximum=600,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Vault download timeout",
        consequence=(
            "How long one download from Google's export storage may take "
            "before it is abandoned and resumed."
        ),
    ),
    SettingSpec(
        key="PRR_OBJECT_STORE_CONNECT_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=5,
        minimum=1,
        maximum=30,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Record storage connect timeout",
        consequence=(
            "How long the application waits to reach record storage before "
            "treating it as unavailable."
        ),
    ),
    SettingSpec(
        key="PRR_OBJECT_STORE_READ_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=120,
        unit="seconds",
        category=CATEGORY_RECORDS,
        label="Record storage read timeout",
        consequence=(
            "How long one read from record storage may take before it is "
            "abandoned and retried."
        ),
    ),
    SettingSpec(
        key="PRR_OBJECT_STORE_MAX_ATTEMPTS",
        type=values.INTEGER,
        default=3,
        minimum=1,
        maximum=5,
        unit="attempts",
        category=CATEGORY_RECORDS,
        label="Record storage retry attempts",
        consequence=(
            "How many times a record-storage operation is retried before it "
            "fails. It never causes a partial write: a record is stored "
            "whole or not at all."
        ),
    ),
    SettingSpec(
        key="PRR_VERIFIED_OBJECT_MEMORY_BYTES",
        type=values.INTEGER,
        default=8 * _MIB,
        minimum=_MIB,
        maximum=64 * _MIB,
        unit="bytes",
        category=CATEGORY_RECORDS,
        label="Digest verification memory",
        consequence=(
            "How much of a record is held in memory while its digest is "
            "verified; anything larger is verified on disk. Every record is "
            "verified before it is served either way."
        ),
    ),
    # ------------------------------------------------------ access and tokens
    SettingSpec(
        key="PRR_STAFF_LOGIN_RATE",
        type=values.RATE,
        default="10/min",
        periods=_SHORT_PERIODS,
        category=CATEGORY_ACCESS,
        label="Staff login rate",
        consequence=(
            "How often a staff sign-in may be attempted from one address. "
            "Lowering it can lock staff out during an incident, which is the "
            "moment they most need in."
        ),
    ),
    SettingSpec(
        key="PRR_PERSONAL_TOKEN_CREATE_RATE",
        type=values.RATE,
        default="10/hour",
        periods=_SHORT_PERIODS,
        category=CATEGORY_ACCESS,
        label="Personal token creation rate",
        consequence=(
            "How often one staff member may mint a personal API token."
        ),
    ),
    SettingSpec(
        key="PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS",
        type=values.INTEGER,
        default=90,
        minimum=1,
        maximum=365,
        unit="days",
        category=CATEGORY_ACCESS,
        label="Personal token maximum lifetime",
        consequence=(
            "The longest a personal API token may be valid for. Tokens "
            "already issued keep the expiry they were given; this bounds new "
            "ones."
        ),
    ),
    SettingSpec(
        key="PRR_PERSONAL_TOKEN_MAX_ACTIVE",
        type=values.INTEGER,
        default=5,
        minimum=1,
        maximum=25,
        unit="tokens",
        category=CATEGORY_ACCESS,
        label="Active personal tokens per person",
        consequence=(
            "How many personal API tokens one staff member may hold at once. "
            "Lowering it does not revoke tokens already issued."
        ),
    ),
    SettingSpec(
        key="PRR_PERSONAL_TOKEN_LAST_USED_WINDOW_SECONDS",
        type=values.INTEGER,
        default=60,
        minimum=0,
        maximum=3600,
        unit="seconds",
        category=CATEGORY_ACCESS,
        label="Token last-used write window",
        consequence=(
            "How coarsely a token's last-used time is recorded, so a busy "
            "agent does not write on every call. A larger window makes "
            "'last used' less precise when someone is auditing a token."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_DOWNLOAD_PER_IP_RATE",
        type=values.RATE,
        default="120/hour",
        periods=_SHORT_PERIODS,
        category=CATEGORY_ACCESS,
        label="Installment download rate",
        consequence=(
            "How often one address may fetch released installment files. "
            "Lowering it can stop a requester collecting a large production "
            "they are entitled to."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_ACCESS_CODE_PER_IP_RATE",
        type=values.RATE,
        default="20/hour",
        periods=_SHORT_PERIODS,
        category=CATEGORY_ACCESS,
        label="Access-code attempts per address",
        consequence=(
            "How often one address may try an installment access code. A "
            "requester who has the code types it once; anyone trying it "
            "repeatedly is guessing."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_ACCESS_CODE_PER_INSTALLMENT_RATE",
        type=values.RATE,
        default="50/hour",
        periods=_SHORT_PERIODS,
        category=CATEGORY_ACCESS,
        label="Access-code attempts per installment",
        consequence=(
            "The same ceiling from the link's side, so a pool of addresses "
            "cannot outrun the per-address limit against one leaked link."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS",
        type=values.INTEGER,
        default=7,
        minimum=1,
        maximum=30,
        unit="days",
        category=CATEGORY_ACCESS,
        label="Installment link lifetime",
        consequence=(
            "How long a requester's download link stays usable. RCW "
            "42.56.120 lets the district set a reasonable period; a short "
            "one means a requester who is away misses the production and has "
            "to ask for it again."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_ACCESS_CODE_MIN_LENGTH",
        type=values.INTEGER,
        default=8,
        minimum=6,
        maximum=64,
        unit="characters",
        category=CATEGORY_ACCESS,
        label="Access-code minimum length",
        consequence=(
            "The shortest access code an officer may set on an installment. "
            "Codes already set keep working."
        ),
    ),
    SettingSpec(
        key="PRR_INSTALLMENT_MAX_ITEMS",
        type=values.INTEGER,
        default=5000,
        minimum=1,
        maximum=50000,
        unit="records",
        category=CATEGORY_ACCESS,
        label="Records per installment",
        consequence=(
            "A ceiling on one installment's membership. Installments are the "
            "lever for a large production under RCW 42.56.080(2), so this is "
            "deliberately generous; it exists to keep one mis-aimed "
            "select-all from building an unshippable download."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_GROUP_RECHECK_SECONDS",
        type=values.INTEGER,
        default=900,
        minimum=60,
        maximum=86400,
        unit="seconds",
        category=CATEGORY_ACCESS,
        label="Group membership recheck interval",
        consequence=(
            "How often a signed-in staff member's Google group membership is "
            "re-read. A longer interval means someone removed from a role "
            "group keeps that role for up to this long."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_GROUP_MAX_STALE_SECONDS",
        type=values.INTEGER,
        default=1800,
        minimum=60,
        maximum=604800,
        unit="seconds",
        category=CATEGORY_ACCESS,
        label="Group membership maximum staleness",
        consequence=(
            "How old a group membership answer may be before the session is "
            "ended rather than trusted. It is what makes a Google outage "
            "sign staff out instead of leaving stale roles in place, so a "
            "long value trades revocation speed for staying signed in."
        ),
    ),
    # ------------------------------------------------------ Google identities
    #
    # Phase C moves the identities the application acts as, not private keys.
    # Deployed Gmail and Vault access is keyless: the container authenticates
    # as itself and impersonates a named service account, and a deployed
    # private key stays forbidden (rules 11 and 12). What an administrator
    # rotates here is therefore *which* identity is used -- the thing that
    # actually changes when the district re-issues a service account -- while
    # the key material the application never holds stays absent.
    SettingSpec(
        key="PRR_GMAIL_IMPERSONATION_TARGET",
        type=values.EMAIL,
        default="",
        allow_blank=True,
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Gmail service identity",
        consequence=(
            "The service account the application impersonates to read and "
            "send the district mailbox. Blank means no deployed Gmail "
            "transport at all: nothing is sent to a requester and nothing is "
            "polled, while every statutory clock keeps running."
        ),
    ),
    SettingSpec(
        key="PRR_GMAIL_CREDENTIALS_FILE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"/[A-Za-z0-9._/-]{1,254}",
        format_message=(
            "A credentials file is an absolute path, such as "
            "/run/secrets/prr-gmail.json."
        ),
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Gmail key file (local development only)",
        consequence=(
            "A local service-account key file for a developer's machine. A "
            "deployed instance refuses to use one — deployed Gmail access is "
            "keyless impersonation — so setting it outside development "
            "changes nothing except to make the transport refuse."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_IMPERSONATION_TARGET",
        type=values.EMAIL,
        default="",
        allow_blank=True,
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Vault service identity",
        consequence=(
            "The service account the application impersonates for Google "
            "Vault matters, holds, counts, and exports. Blank means no "
            "search of district mail can run: collection stops, and a "
            "records officer has to search by hand."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_ADMIN_USER",
        type=values.EMAIL,
        default="",
        allow_blank=True,
        bootstrap_from=("VAULT_ADMIN_USER",),
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Vault administrator account",
        consequence=(
            "The Workspace account the Vault identity acts as. It must be an "
            "account with Vault privileges; the wrong one makes every Vault "
            "call fail rather than silently return less."
        ),
    ),
    SettingSpec(
        key="PRR_VAULT_CREDENTIALS_FILE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"/[A-Za-z0-9._/-]{1,254}",
        format_message=(
            "A credentials file is an absolute path, such as "
            "/run/secrets/prr-vault.json."
        ),
        bootstrap_from=("GOOGLE_SERVICE_ACCOUNT_FILE",),
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Vault key file (local development only)",
        consequence=(
            "A local service-account key file for a developer's machine. A "
            "deployed instance refuses to use one; deployed Vault access is "
            "keyless impersonation."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_GROUP_CREDENTIALS_FILE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"/[A-Za-z0-9._/-]{1,254}",
        format_message=(
            "A credentials file is an absolute path, such as "
            "/run/secrets/prr-google-groups-credential.json."
        ),
        category=CATEGORY_GOOGLE_IDENTITY,
        label="Google group reader credential file",
        consequence=(
            "The keyless credential configuration the read-only group reader "
            "uses to resolve staff roles from Workspace groups. Blank falls "
            "back to the container's own identity; a wrong path makes group "
            "lookups fail, which ends sessions rather than granting roles."
        ),
    ),
    # ----------------------------------------------------------- Staff sign-in
    SettingSpec(
        key="PRR_AUTH_MODE",
        type=values.CHOICE,
        default="google",
        choices=("google", "local"),
        category=CATEGORY_SIGN_IN,
        label="Staff sign-in mode",
        consequence=(
            "How staff sign in. 'google' is the deployed mode: Google "
            "accounts in the district's Workspace domain. 'local' accepts "
            "Django passwords and exists for a developer's machine — "
            "choosing it on a deployed instance turns district sign-in into "
            "a password list."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_OIDC_CLIENT_ID",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        category=CATEGORY_SIGN_IN,
        label="Google sign-in client ID",
        consequence=(
            "The OAuth client staff sign-in presents to Google. Wrong or "
            "blank, every staff member is refused at the Google screen and "
            "nobody can reach a case."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_OIDC_CLIENT_SECRET",
        type=values.SECRET,
        default="",
        allow_blank=True,
        max_length=255,
        category=CATEGORY_SIGN_IN,
        label="Google sign-in client secret",
        consequence=(
            "The secret paired with the sign-in client ID. It is write-only: "
            "it can be replaced or cleared, never read back. A wrong value "
            "fails every sign-in at the token exchange."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_HOSTED_DOMAIN",
        type=values.DOMAIN,
        default="psd401.net",
        category=CATEGORY_SIGN_IN,
        label="Workspace sign-in domain",
        consequence=(
            "The only Workspace domain whose accounts may sign in. Changing "
            "it to a domain the district's staff are not in locks every one "
            "of them out."
        ),
    ),
    SettingSpec(
        key="PRR_GOOGLE_OIDC_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=10,
        minimum=1,
        maximum=30,
        unit="seconds",
        category=CATEGORY_SIGN_IN,
        label="Google sign-in request timeout",
        consequence=(
            "How long a sign-in waits on Google before giving up. Too short "
            "turns a slow Google into a failed sign-in; too long leaves "
            "staff watching a spinner during an outage."
        ),
    ),
    SettingSpec(
        key="PRR_ENABLE_ADMIN",
        type=values.BOOLEAN,
        default=False,
        category=CATEGORY_SIGN_IN,
        label="Django admin break-glass route",
        consequence=(
            "On, the superuser-only Django admin route answers. It is an "
            "incident escape hatch that bypasses the staff application's "
            "role checks, so it is off except while one is in progress."
        ),
    ),
    SettingSpec(
        key="PRR_BREAK_GLASS_PASSWORD_AUTH",
        type=values.BOOLEAN,
        default=False,
        requires=("PRR_ENABLE_ADMIN",),
        category=CATEGORY_SIGN_IN,
        label="Break-glass password sign-in",
        consequence=(
            "On, a Django password may be used to sign in even while Google "
            "is the sign-in mode. It is the way back in when Google sign-in "
            "is broken, and it is a password door into the district's "
            "records while it is open."
        ),
    ),
    # ----------------------------------------------------------- Operations
    SettingSpec(
        key="PRR_CLOUDWATCH_REGION",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=64,
        pattern=r"[a-z0-9-]{1,64}",
        format_message="A region is a name such as us-west-2.",
        bootstrap_from=("AWS_DEFAULT_REGION", "AWS_REGION"),
        category=CATEGORY_OPERATIONS,
        label="CloudWatch region",
        consequence=(
            "Where delivery and queue metrics are published. Blank means no "
            "metrics leave the application, so an outage is noticed by a "
            "person rather than by an alarm."
        ),
    ),
    SettingSpec(
        key="PRR_OFFICE_CONVERTER_BINARY",
        type=values.TEXT,
        default="/usr/bin/soffice",
        max_length=255,
        pattern=r"/[A-Za-z0-9._/-]{1,254}",
        format_message=(
            "The converter is an absolute path, such as /usr/bin/soffice."
        ),
        category=CATEGORY_OPERATIONS,
        label="Office document converter",
        consequence=(
            "The program that renders Word, Excel, and PowerPoint records "
            "for review. A wrong path means those records cannot be "
            "previewed or redacted and have to be handled another way."
        ),
    ),
    # ------------------------------------- Portal, addresses, and the clock
    SettingSpec(
        key="PORTAL_BASE_URL",
        type=values.TEXT,
        default="https://prr.psd401.net",
        max_length=255,
        pattern=(
            r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?"
            r"|http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?"
        ),
        format_message=(
            "The portal address is an HTTPS origin with no path, such as "
            "https://prr.psd401.net."
        ),
        category=CATEGORY_CORRESPONDENCE,
        label="Portal address in requester links",
        consequence=(
            "The address every status link and installment download link in "
            "requester correspondence points at. Wrong, requesters are sent "
            "links that do not open, and installment notices are refused "
            "until it matches the address downloads are served from."
        ),
    ),
    SettingSpec(
        key="PRA_SUPERVISOR_EMAIL",
        type=values.EMAIL,
        default="publicrecords@psd401.net",
        category=CATEGORY_DOMAINS,
        label="Public records supervisor address",
        consequence=(
            "Who is copied on deadline escalations, footage preservation, "
            "and stalled-case alerts. A wrong address means the person "
            "accountable for a missed deadline is not told about it."
        ),
    ),
    SettingSpec(
        key="PRA_OFFICER_EMAIL",
        bootstrap_from=("PRA_SUPERVISOR_EMAIL",),
        type=values.EMAIL,
        default="publicrecords@psd401.net",
        category=CATEGORY_DOMAINS,
        label="Records officer contact address",
        consequence=(
            "The contact address printed in requester correspondence and on "
            "installment download pages. Requesters write to it with "
            "questions and clarifications, so it must be a monitored "
            "mailbox."
        ),
    ),
    SettingSpec(
        key="FACILITIES_EMAIL",
        bootstrap_from=("PRA_SUPERVISOR_EMAIL",),
        type=values.EMAIL,
        default="publicrecords@psd401.net",
        category=CATEGORY_DOMAINS,
        label="Facilities preservation address",
        consequence=(
            "Who receives the urgent manual footage-hold instructions when "
            "the camera system cannot place a hold itself. Wrong, footage "
            "can age out before anyone preserves it."
        ),
    ),
    SettingSpec(
        key="DISTRICT_HOLIDAYS",
        type=values.DATE_LIST,
        default=(),
        allow_blank=True,
        max_items=100,
        category=CATEGORY_COMPLIANCE,
        label="District closure days",
        consequence=(
            "Days the district is closed beyond Washington's legal "
            "holidays. They are not business days under RCW 42.56.520, so "
            "every deadline computed after a change counts around them. "
            "Deadlines already set are not recomputed until the case next "
            "changes status. Listing a day the district was open extends "
            "deadlines the law did not extend."
        ),
    ),
    SettingSpec(
        key="DEFAULT_ESTIMATE_BUSINESS_DAYS",
        type=values.INTEGER,
        default=60,
        minimum=1,
        maximum=365,
        unit="business days",
        category=CATEGORY_COMPLIANCE,
        label="Default response estimate",
        consequence=(
            "The estimate recorded on an acknowledgment drafted without a "
            "date, including the day-five safety net's. The counsel-approved "
            "acknowledgment says \"60 business days\" in its own words, so "
            "changing this without changing that letter makes the case "
            "track one horizon while the requester was promised another."
        ),
    ),
    SettingSpec(
        key="STALL_THRESHOLD_DAYS",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=365,
        unit="days",
        category=CATEGORY_COMPLIANCE,
        label="Stalled case threshold",
        consequence=(
            "A request with no substantive activity for this many days is "
            "flagged stalled and the supervisor is alerted. Longer means an "
            "abandoned case is noticed later, which is how a response "
            "stops being diligent."
        ),
    ),
    SettingSpec(
        key="PRR_INBOX_TRIAGE_ALERT_BUSINESS_DAYS",
        type=values.INTEGER,
        default=1,
        minimum=1,
        maximum=10,
        unit="business days",
        category=CATEGORY_STAFF,
        label="Untriaged inbound mail alert",
        consequence=(
            "Inbound mail that opened no case and matched no case with "
            "certainty, still waiting after this many business days, is "
            "named in a weekday-morning alert to the records officers. "
            "Longer means a request sent to an old or secondary address is "
            "noticed later, while its statutory clock is already running."
        ),
    ),
    # ------------------------------------------------------- Integrations
    SettingSpec(
        key="CLAMAV_HOST",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=253,
        pattern=r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
        format_message="The scanner address is a host name, such as clamav.",
        category=CATEGORY_INTEGRATIONS,
        label="Malware scanner host",
        consequence=(
            "Where every uploaded file is sent to be scanned before anyone "
            "may open it. Blank or wrong, uploads stay quarantined as "
            "pending: nothing infected is released, and nothing clean is "
            "either."
        ),
    ),
    SettingSpec(
        key="CLAMAV_PORT",
        type=values.INTEGER,
        default=3310,
        minimum=1,
        maximum=65535,
        category=CATEGORY_INTEGRATIONS,
        label="Malware scanner port",
        consequence=(
            "The port the scanner listens on. A wrong port looks exactly "
            "like a scanner that is down: uploads wait in quarantine."
        ),
    ),
    SettingSpec(
        key="CLAMAV_TIMEOUT_SECONDS",
        type=values.INTEGER,
        default=30,
        minimum=1,
        maximum=120,
        unit="seconds",
        category=CATEGORY_INTEGRATIONS,
        label="Malware scan timeout",
        consequence=(
            "How long one scan may take. Too short fails large files as "
            "unscannable, so they stay quarantined; too long holds a worker "
            "while the scanner is stuck."
        ),
    ),
    SettingSpec(
        key="ALTA_API_BASE",
        type=values.TEXT,
        default="",
        allow_blank=True,
        max_length=255,
        pattern=r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?",
        format_message=(
            "The Alta address is an HTTPS URL with no credentials or query, "
            "such as https://psd401.alta.avigilon.com/api/v1."
        ),
        category=CATEGORY_INTEGRATIONS,
        label="Avigilon Alta API address",
        consequence=(
            "Where camera-footage holds are placed. Blank or wrong, every "
            "footage request falls back to an urgent manual hold for "
            "facilities, and footage can age out while that waits."
        ),
    ),
    SettingSpec(
        key="ALTA_API_TOKEN",
        type=values.SECRET,
        default="",
        allow_blank=True,
        max_length=4096,
        category=CATEGORY_INTEGRATIONS,
        label="Avigilon Alta API token",
        consequence=(
            "The bearer token footage holds are placed with. It is "
            "write-only: it can be replaced or cleared, never read back. "
            "Blank or revoked, holds fall back to manual facilities work."
        ),
    ),
)

#: Ordering constraints between two settings, checked from either side on
#: the save path. A combined ceiling that falls below its per-item ceiling
#: would be a contradiction that only shows up as a confusing refusal much
#: later, in front of a requester's upload.
RELATIONS: tuple[Relation, ...] = (
    Relation(
        greater="PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES",
        lesser="PRR_INTAKE_ATTACHMENT_MAX_BYTES",
        message=(
            "The public form's combined file size must be at least its "
            "per-file size."
        ),
    ),
    Relation(
        greater="PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES",
        lesser="PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES",
        message=(
            "The combined attachment size must be at least the per-file "
            "size."
        ),
    ),
    Relation(
        greater="PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES",
        lesser="PRR_RECORD_UPLOAD_MAX_BYTES",
        message=(
            "The staff record upload combined size must be at least its "
            "per-file size."
        ),
    ),
    Relation(
        greater="PRR_VAULT_EXPORT_TOTAL_MAX_BYTES",
        lesser="PRR_VAULT_EXPORT_MAX_BYTES",
        message=(
            "The combined Vault export size must be at least the per-object "
            "size."
        ),
    ),
    Relation(
        greater="PRR_GOOGLE_GROUP_MAX_STALE_SECONDS",
        lesser="PRR_GOOGLE_GROUP_RECHECK_SECONDS",
        message=(
            "The maximum group-membership staleness must not be shorter than "
            "the recheck interval, or every session would end on its first "
            "recheck."
        ),
    ),
)

SETTINGS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}

SETTING_KEYS: tuple[str, ...] = tuple(spec.key for spec in SETTINGS)

#: Category display order for the Settings page. Correspondence and the
#: statutory safety net come first because those are the switches whose wrong
#: value a requester feels.
CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_CORRESPONDENCE,
    CATEGORY_COMPLIANCE,
    CATEGORY_GMAIL,
    CATEGORY_INTAKE,
    CATEGORY_DOMAINS,
    CATEGORY_STAFF,
    CATEGORY_RECORDS,
    CATEGORY_ACCESS,
    CATEGORY_ADVISORY_AI,
    CATEGORY_OBSERVABILITY,
    CATEGORY_SIGN_IN,
    CATEGORY_GOOGLE_IDENTITY,
    CATEGORY_OPERATIONS,
    CATEGORY_INTEGRATIONS,
)

#: Settings whose plaintext is never stored, never returned, and never
#: written into a history row. They live in ``AppSecret``, encrypted.
SECRET_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in SETTINGS if spec.type == values.SECRET
)


def spec_for(key: str) -> SettingSpec:
    try:
        return SETTINGS_BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"{key} is not an application-managed setting.") from exc


def dependents_of(key: str) -> tuple[str, ...]:
    """Keys that may not stay on once ``key`` is turned off."""
    return tuple(spec.key for spec in SETTINGS if key in spec.requires)


def relations_for(key: str) -> tuple[Relation, ...]:
    """Every ordering constraint ``key`` takes part in, from either side."""
    return tuple(
        relation
        for relation in RELATIONS
        if key in (relation.greater, relation.lesser)
    )


def _validate_registry() -> None:
    """Catch a registry mistake at import rather than at an admin's save."""
    seen: set[str] = set()
    for spec in SETTINGS:
        if spec.key in seen:
            raise ValueError(f"{spec.key} is registered twice.")
        seen.add(spec.key)
        if spec.category not in CATEGORY_ORDER:
            raise ValueError(f"{spec.key} is in unlisted category {spec.category!r}.")
        for required in spec.requires:
            if required not in seen and required not in {s.key for s in SETTINGS}:
                raise ValueError(f"{spec.key} requires unknown setting {required}.")
        # The default must itself be a value the save path would accept, so a
        # fail-closed read can never hand a consumer something invalid.
        values.coerce(spec, spec.default)
    for relation in RELATIONS:
        for side in (relation.greater, relation.lesser):
            if side not in seen:
                raise ValueError(f"RELATIONS names unknown setting {side}.")


_validate_registry()
