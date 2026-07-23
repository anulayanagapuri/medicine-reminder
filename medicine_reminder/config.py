"""
Configuration file for the Medicine Reminder app.

TWILIO
------
The Account SID below is taken from your Twilio API Explorer. You still need
to provide the AUTH TOKEN (it is masked in the console) and ONE sender:

  * Either a Messaging Service SID (recommended â€“ your console had the
    "Messaging Service" sender type selected), OR
  * A Twilio "From" phone number.

Best practice is to supply secrets via environment variables of the same name
rather than hard-coding them here.

Recipient number: it is NOT hard-coded. Each reminder is sent to the phone
number stored on the registered user who owns that medicine. The seeded admin
user is pre-filled with your verified test number +919866212932.

If credentials are incomplete the app still runs â€” SMS is logged (dry-run)
instead of dispatched, so you can demo the whole flow safely.
"""

import os


class Config:
    # ---- Flask core ----
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

    # ---- Database ----
    DB_NAME = os.environ.get("DB_NAME", "medicine_reminder.db")

    # ---- Twilio (SMS) ----
    # Account SID from your console screenshot:
    TWILIO_ACCOUNT_SID = os.environ.get(
        "TWILIO_ACCOUNT_SID", "AC09db59d82fc81e65d7bc89f44b1c2552"
    )
    # Masked in your screenshot â€“ paste it here or set the env var:
    #Org SID : ORb96d9ad7aa197e81e5cf73734618c8d8
    #User SID: US4c36229fbcbe65e8bb212ad38c5f63eb
    #Message MSID: MGd2a2a66e9813b94bd7bcee125fc6e733
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "28d180493cd00d09b92190d5feeb0bfc")

    # Pick ONE sender. Your console used a Messaging Service, so prefer this:
    TWILIO_MESSAGING_SERVICE_SID = os.environ.get(
        "TWILIO_MESSAGING_SERVICE_SID", "MGd2a2a66e9813b94bd7bcee125fc6e733"  # e.g. "MGxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    # ...or fall back to a Twilio phone number if you are not using a service:
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")  # e.g. "+12025550123"

    # ---- SMS template (2 lines, includes the medicine name) ----
    # Available placeholders: {name}, {dosage}
    SMS_TEMPLATE = os.environ.get(
        "SMS_TEMPLATE",
        "Medicine Reminder! \nIt's time to take your {name} ({dosage}).-MRA" 
        #"Stay healthy!",
    )

    # ---- Reminder scheduler ----
    # How often (seconds) the background scheduler checks for due medicines.
    # Reminders still fire only ONCE per scheduled time per day (de-duplicated),
    # regardless of how often the scheduler ticks.
    SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "30"))

    @classmethod
    def _sender_ready(cls):
        return bool(cls.TWILIO_MESSAGING_SERVICE_SID) or bool(cls.TWILIO_FROM_NUMBER)

    @classmethod
    def twilio_is_configured(cls):
        """True only when SID + auth token + one valid sender are all present."""
        placeholders = {"your_auth_token_here", "", None}
        return (
            cls.TWILIO_ACCOUNT_SID not in placeholders
            and cls.TWILIO_AUTH_TOKEN not in placeholders
            and cls._sender_ready()
        )

    @classmethod
    def build_sms_body(cls, name, dosage):
        """Render the 2-line template with the medicine details."""
        return cls.SMS_TEMPLATE.format(name=name or "medicine", dosage=dosage or "as prescribed")
