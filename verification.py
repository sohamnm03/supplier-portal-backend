"""PAN, GSTIN and Aadhaar verification through the third-party verification API.

The endpoint URLs live in verification_endpoints.json (service name -> URL), which is read on every request, so
they can be changed while the server is running. Set VERIFICATION_ENDPOINTS_FILE to use a different file.

The browser never sees the raw responses: the company name registered against a PAN / GSTIN and the address on an
Aadhaar stay on the server. Only the GSTIN result hands back what the form is allowed to fill in (legal name,
address, state, PIN, PAN); an Aadhaar is verified and nothing else.
"""
import json
import os
import re
from pathlib import Path

import requests

ENDPOINTS_FILE = Path(os.getenv("VERIFICATION_ENDPOINTS_FILE") or Path(__file__).with_name("verification_endpoints.json"))
TIMEOUT_SECONDS = float(os.getenv("VERIFICATION_TIMEOUT_SECONDS", "10"))

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
AADHAAR_RE = re.compile(r"^[2-9][0-9]{11}$")

# service -> (name of the field in the request body, normaliser, shape check, label)
SERVICES = {
    "PAN": ("pan", lambda v: str(v or "").strip().upper(), PAN_RE, "PAN"),
    "GSTIN": ("gstin", lambda v: str(v or "").strip().upper(), GSTIN_RE, "GSTIN"),
    "AADHAAR": ("aadhaar", lambda v: re.sub(r"\D", "", str(v or "")), AADHAAR_RE, "Aadhaar number"),
}

UNAVAILABLE_MESSAGE = "Verification is unavailable right now. Please try again shortly."


def normalise(service, value):
    return SERVICES[service][1](value)


def is_well_formed(service, value):
    return bool(SERVICES[service][2].match(normalise(service, value)))


class VerificationUnavailable(Exception):
    """The verification service could not be reached or answered with something unusable."""


def _endpoint(service):
    try:
        endpoints = json.loads(ENDPOINTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise VerificationUnavailable(f"Could not read {ENDPOINTS_FILE.name}: {err}") from err
    url = endpoints.get(service) if isinstance(endpoints, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise VerificationUnavailable(f"No URL configured for {service} in {ENDPOINTS_FILE.name}")
    return url.strip()


def call(service, value):
    """Run one value through the verification API. Returns {"verified": bool, "message": str, "data": dict}.

    Raises VerificationUnavailable when the service is down, times out or returns something that is not the
    documented shape. A definite "no active record" answer is a normal result, not an exception.
    """
    field = SERVICES[service][0]
    url = _endpoint(service)
    try:
        response = requests.post(url, json={field: normalise(service, value)}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as err:
        raise VerificationUnavailable(str(err)) from err

    try:
        body = response.json()
    except ValueError as err:
        raise VerificationUnavailable(f"{service} verification returned a non-JSON response ({response.status_code})") from err
    if response.status_code >= 500 or not isinstance(body, dict) or not isinstance(body.get("verified"), bool):
        raise VerificationUnavailable(f"{service} verification returned an unexpected response ({response.status_code})")

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return {"verified": body["verified"], "message": str(body.get("message") or ""), "data": data}


def _name_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _pan_of_gstin(gstin):
    """The PAN written inside a GSTIN (characters 3-12). By how GSTINs are issued this is the holder's PAN, so it is what we trust; the
    provider's own pan_number field is not used because a record's copy can disagree with the GSTIN itself."""
    return gstin[2:12]


PAN_MISMATCH_MESSAGE = "This PAN does not match the GSTIN. The PAN inside GSTIN {gstin} is {embedded}."
NAME_MISMATCH_MESSAGE = "The name on this PAN's record does not match the name registered for the GSTIN."


def check_pan_belongs_to_gstin(pan, gstin, gstin_data, pan_data=None):
    """Return an error message unless `pan` is the PAN this GSTIN is registered under, else None.

    A company has many GSTINs but one PAN, so the PAN inside the GSTIN must be the PAN given, and when both services name the
    registered holder those names must agree.
    """
    if _pan_of_gstin(gstin) != pan:
        return PAN_MISMATCH_MESSAGE.format(gstin=gstin, embedded=_pan_of_gstin(gstin))
    if pan_data:
        gst_name, pan_name = _name_key(gstin_data.get("name")), _name_key(pan_data.get("name"))
        if gst_name and pan_name and gst_name != pan_name:
            return NAME_MISMATCH_MESSAGE
    return None


def _result(verified, message, status=200, **extra):
    return {"verified": verified, "message": message, **extra}, status


def verify_for_form(service, value, pan=None, gstin=None):
    """What the form's Verify button needs. Returns (body, http_status); the body never carries raw API data.

    `pan` / `gstin` are the other tax identifiers currently in the form, used to check that the PAN belongs to
    the GSTIN. For a GSTIN, "fill" holds the only values the form may auto-fill.
    """
    label = SERVICES[service][3]
    value = normalise(service, value)
    if not value:
        return _result(False, f"Enter a {label} first.", 400)
    if not is_well_formed(service, value):
        return _result(False, f"Enter a valid {label}.", 400)

    try:
        outcome = call(service, value)
        if not outcome["verified"]:
            return _result(False, outcome["message"] or f"No active record found for this {label}.")

        if service == "GSTIN":
            return _verified_gstin(value, outcome["data"])
        if service == "PAN":
            other = normalise("GSTIN", gstin)
            if other and is_well_formed("GSTIN", other):
                gstin_outcome = call("GSTIN", other)
                if gstin_outcome["verified"]:
                    error = check_pan_belongs_to_gstin(value, other, gstin_outcome["data"], outcome["data"])
                    if error:
                        return _result(False, error)
    except VerificationUnavailable:
        return _result(False, UNAVAILABLE_MESSAGE, 503)
    return _result(True, f"{label} verified.")


def _verified_gstin(gstin, data):
    """A verified (active) GSTIN fills in its details and the PAN written inside it. The GSTIN is verified on its own merits: whether
    that PAN can ALSO be verified is reported separately (pan_verified, and pan_message for the PAN field), never as a GSTIN error."""
    pan = _pan_of_gstin(gstin)
    pan_message = ""
    pan_verified = False
    try:
        pan_outcome = call("PAN", pan) if PAN_RE.match(pan) else None
        if pan_outcome is None:
            pan_message = f"The PAN inside this GSTIN ({pan}) is not a valid PAN."
        elif not pan_outcome["verified"]:
            pan_message = f"PAN {pan} (from your GSTIN) was not found in the PAN records."
        else:
            error = check_pan_belongs_to_gstin(pan, gstin, data, pan_outcome["data"])
            pan_verified = error is None
            pan_message = error or ""
    except VerificationUnavailable:
        pan_message = "PAN verification is unavailable right now. Please press Verify on the PAN in a moment."

    fill = {
        "vendor_legal_name": str(data.get("trade_name") or data.get("name") or "").strip(),
        "street": str(data.get("address") or "").strip(),
        "state": str(data.get("state") or "").strip(),
        "postal_code": str(data.get("pincode") or "").strip(),
        "pan": pan,
    }
    return _result(True, "GSTIN verified.", fill=fill, pan_verified=pan_verified, pan_message=pan_message)


def enforce_on_submit(values):
    """Server-side guarantee for submit: every PAN / GSTIN / Aadhaar that is filled in must verify right now.

    `values` uses the vendor column names (pan, gstin, aadhaar_no). Returns (message, http_status) or None.
    Fails closed: if the verification service is down the application cannot be submitted.
    """
    pan = normalise("PAN", values.get("pan"))
    gstin = normalise("GSTIN", values.get("gstin"))
    aadhaar = normalise("AADHAAR", values.get("aadhaar_no"))
    checks = [("PAN", pan), ("GSTIN", gstin), ("AADHAAR", aadhaar)]
    try:
        gstin_data = pan_data = None
        for service, value in checks:
            if not value:
                continue
            label = SERVICES[service][3]
            if not is_well_formed(service, value):
                return f"Enter a valid {label}.", 400
            outcome = call(service, value)
            if not outcome["verified"]:
                return outcome["message"] or f"The {label} could not be verified.", 422
            if service == "GSTIN":
                gstin_data = outcome["data"]
            elif service == "PAN":
                pan_data = outcome["data"]
        if pan and gstin:
            error = check_pan_belongs_to_gstin(pan, gstin, gstin_data or {}, pan_data)
            if error:
                return error, 422
    except VerificationUnavailable:
        return UNAVAILABLE_MESSAGE, 503
    return None
