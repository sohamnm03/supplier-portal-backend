"""The vendor agreement as a PDF.

The same module lives in both backends (vendor-enterprise-app/backend/app/agreement_pdf.py and
supplier-portal-backend/agreement_pdf.py) and must be kept identical: the enterprise app builds the unsigned agreement when the checker
issues it, and the vendor portal builds the signed copy (vendor signature and a signing certificate page) from the very same saved terms.

Everything the document is made of is in one `terms` dict, saved with the agreement (vendor_agreement.terms_json), so the signed
document can always be rebuilt and later edits to the clause master never change an issued agreement:

    {
      "agreement_no": "AGR-2026-00001", "version": 1, "agreement_date": "19 Oct 2026", "place": "Mumbai", "effective_date": "...",
      "company": {"name", "address", "signatory_name", "signatory_designation", "notice_email"},
      "vendor": {"legal_name", "type", "address", "pan", "gstin", "email"},
      "terms": {"term_months", "payment_days", "notice_days", "confidentiality_years", "arbitration_city", "jurisdiction_city"},
      "clauses": [{"no": 1, "title": "Definitions", "body": "... already filled in ..."}],
    }

Signatures are PNG images (see normalise_signature): the drawn, typed (rendered in the browser) or uploaded one, and the company's.
"""
import hashlib
import io
import re
from datetime import date
from xml.sax.saxutils import escape

from PIL import Image
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Image as FlowImage
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

NAVY = colors.HexColor("#102a4c")
BLUE = colors.HexColor("#1265c8")
GREY = colors.HexColor("#59728f")
RULE = colors.HexColor("#c9d6e6")
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

MAX_SIGNATURE_BYTES = 600 * 1024
SIGNATURE_BOX = (62 * mm, 22 * mm)  # width, height of the signature area in the signing block


def _pretty_date(iso):
    """'2028-10-19' as '19 Oct 2028' ('' when it is not a date)."""
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return ""


def _tenure(terms):
    """(effective date text, end date text, term text) of the agreement, whatever of it the saved terms hold."""
    start = terms.get("effective_date") or _pretty_date(terms.get("start_iso"))
    end = _pretty_date(terms.get("end_iso"))
    months = (terms.get("terms") or {}).get("term_months")
    return start, end, (f"{months} months" if months else "")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def fill_placeholders(text, values):
    """Replace {{name}} with its value; an unknown placeholder is left visibly empty rather than printed as code."""
    return PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), "") or ""), text or "")


def normalise_signature(data):
    """A signature image (PNG or JPEG bytes) as a clean transparent PNG, cropped to the ink and no larger than 600 x 200.

    Raises ValueError for something that is not an image, is too large, or is empty (nothing drawn).
    """
    if not data or len(data) > MAX_SIGNATURE_BYTES:
        raise ValueError("The signature image is missing or too large.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as err:  # Pillow raises several types for bad files
        raise ValueError("The signature is not a valid image.") from err
    image = image.convert("RGBA")
    pixels = image.load()
    width, height = image.size
    # A photo or JPEG of a signature has a white background: make near-white transparent so only the ink remains.
    has_transparency = any(pixels[x, y][3] < 250 for x in range(0, width, max(1, width // 40)) for y in range(0, height, max(1, height // 40)))
    if not has_transparency:
        for x in range(width):
            for y in range(height):
                r, g, b, a = pixels[x, y]
                if r > 235 and g > 235 and b > 235:
                    pixels[x, y] = (r, g, b, 0)
    bbox = image.getchannel("A").point(lambda value: 255 if value > 20 else 0).getbbox()
    if bbox is None:
        raise ValueError("The signature is empty.")
    image = image.crop(bbox)
    image.thumbnail((600, 200))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _styles():
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.6, leading=13.4, textColor=NAVY, alignment=TA_JUSTIFY)
    return {
        "base": base,
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=17, leading=21, alignment=TA_CENTER, spaceAfter=3),
        "sub": ParagraphStyle("sub", parent=base, fontSize=9, textColor=GREY, alignment=TA_CENTER, spaceAfter=10),
        "party": ParagraphStyle("party", parent=base, leftIndent=14, spaceAfter=5),
        "clause": ParagraphStyle("clause", parent=base, spaceAfter=7),
        "small": ParagraphStyle("small", parent=base, fontSize=8.4, leading=11.5, textColor=GREY, alignment=0),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9.2, leading=12.4, alignment=0),
        "head": ParagraphStyle("head", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=14, alignment=0, textColor=BLUE, spaceBefore=4, spaceAfter=6),
    }


class _NumberedCanvas(pdf_canvas.Canvas):
    """Canvas that knows the page count, to print 'Page x of y' and the agreement reference on every page."""

    footer_text = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        width, _ = A4
        self.setStrokeColor(RULE)
        self.setLineWidth(0.6)
        self.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        self.setFont("Helvetica", 7.8)
        self.setFillColor(GREY)
        self.drawString(18 * mm, 10.5 * mm, self.footer_text)
        self.drawRightString(width - 18 * mm, 10.5 * mm, f"Page {self._pageNumber} of {total}")


def _signature_image(png):
    """The signature as a flowable scaled to fit the signature area, or an empty string when there is none."""
    if not png:
        return ""
    reader = ImageReader(io.BytesIO(png))
    iw, ih = reader.getSize()
    scale = min(SIGNATURE_BOX[0] / iw, SIGNATURE_BOX[1] / ih)
    return FlowImage(io.BytesIO(png), width=iw * scale, height=ih * scale, hAlign="LEFT")


def _signing_block(company, vendor, terms, company_png, vendor_png, vendor_name, vendor_designation, vendor_signed_line, styles):
    """Both parties' signature blocks as ONE table, so every row (heading, signature area, rule, name, designation, signing note) sits at the
    same height on both sides whatever the signature images look like."""
    cell, small = styles["cell"], styles["small"]

    def text(style, value, bold_label=None):
        if bold_label:
            return Paragraph(f"<b>{bold_label}</b> {escape(value) if value else '&nbsp;'}", style)
        return Paragraph(escape(value) if value else "&nbsp;", style)

    company_note = f"Signed on behalf of the Company on {terms.get('agreement_date', '')}" if company_png else ""
    rows = [
        [Paragraph(f"<b>For {escape(company['name'])}</b>", cell), "", Paragraph(f"<b>For {escape(vendor['legal_name'])}</b>", cell)],
        [_signature_image(company_png), "", _signature_image(vendor_png)],
        [text(cell, company.get("signatory_name", ""), "Name:"), "", text(cell, vendor_name, "Name:")],
        [text(cell, company.get("signatory_designation", ""), "Designation:"), "", text(cell, vendor_designation, "Designation:")],
        [text(small, company_note), "", text(small, vendor_signed_line or "")],
    ]
    table = Table(rows, colWidths=[82 * mm, 10 * mm, 82 * mm], rowHeights=[None, SIGNATURE_BOX[1] + 3 * mm, None, None, None])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),  # the signature sits on the rule, whatever its height
        ("LINEBELOW", (0, 1), (0, 1), 0.7, NAVY),
        ("LINEBELOW", (2, 1), (2, 1), 0.7, NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 2), (-1, 2), 5),
    ]))
    return table


def _certificate(certificate, terms, document_sha256, verify_url, styles):
    rows = [
        ("Agreement", f"{terms['agreement_no']}  (version {terms.get('version', 1)})"),
        ("Between", f"{terms['company']['name']} and {terms['vendor']['legal_name']}"),
        ("Signed by", certificate.get("signer_name", "")),
        ("Designation", certificate.get("signer_designation", "")),
        ("Signature method", certificate.get("method_label", "")),
        ("Signed at", certificate.get("signed_at_text", "")),
        ("IP address", certificate.get("ip", "")),
        ("Device", certificate.get("user_agent", "")),
        ("Email verified", certificate.get("otp_text", "")),
        ("Details confirmed", certificate.get("confirmed_text", "")),
        ("Document fingerprint (SHA-256 of the agreement as issued)", document_sha256),
    ]
    table = Table([[Paragraph(f"<b>{escape(label)}</b>", styles["cell"]), Paragraph(escape(str(value)), styles["cell"])] for label, value in rows],
                  colWidths=[52 * mm, 118 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f8fc")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    flow = [
        Paragraph("Certificate of Electronic Signing", styles["title"]),
        Paragraph("This page records how the agreement above was signed electronically.", styles["sub"]),
        table,
        Spacer(1, 8),
        Paragraph("The document fingerprint is computed from the exact agreement that was issued to the signer. Any change to the document "
                  "changes the fingerprint, so a copy can be checked against it.", styles["small"]),
    ]
    if verify_url:
        widget = QrCodeWidget(verify_url)
        x1, y1, x2, y2 = widget.getBounds()
        size = 34 * mm
        drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
        drawing.add(widget)
        flow += [Spacer(1, 10), drawing, Paragraph(f"Verify this agreement: {escape(verify_url)}", styles["small"])]
    return flow


def _bold_dates(body_xml, terms):
    """In the term clause's (already escaped) text, set the effective date and the end date in bold."""
    start, end, _ = _tenure(terms)
    for text in {escape(start), escape(end)} - {""}:
        body_xml = body_xml.replace(text, f"<b>{text}</b>")
    return body_xml


def build_agreement_pdf(terms, company_signature_png=None, vendor_signature_png=None, certificate=None, document_sha256=None, verify_url=None):
    """The agreement as PDF bytes.

    Unsigned (no vendor_signature_png): the vendor's signature area is blank. Signed: the vendor's signature is placed in it and, when
    `certificate` is given, a signing certificate page follows (`document_sha256` is the fingerprint of the unsigned agreement).
    certificate keys: signer_name, signer_designation, method_label, signed_at_text, ip, user_agent, otp_text, confirmed_text.
    """
    styles = _styles()
    company, vendor = terms["company"], terms["vendor"]
    buffer = io.BytesIO()

    class Canvas(_NumberedCanvas):
        footer_text = f"{terms['agreement_no']}  |  Vendor Services Agreement  |  {company['name']} and {vendor['legal_name']}"

    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=22 * mm,
                            title=f"Vendor Services Agreement {terms['agreement_no']}", author=company["name"])

    story = [
        Paragraph(escape(company["name"]).upper(), ParagraphStyle("letterhead", parent=styles["base"], fontName="Helvetica-Bold", fontSize=11, textColor=BLUE, alignment=TA_CENTER)),
        Paragraph(escape(company.get("address", "")), styles["sub"]),
        Table([[""]], colWidths=[174 * mm], rowHeights=[1], style=TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.2, BLUE)])),
        Spacer(1, 8),
        Paragraph("VENDOR SERVICES AGREEMENT", styles["title"]),
        Paragraph(f"Agreement No. {escape(terms['agreement_no'])}  |  Version {terms.get('version', 1)}  |  {escape(terms.get('agreement_date', ''))}", styles["sub"]),
        Paragraph(f"This Agreement is made on <b>{escape(terms.get('agreement_date', ''))}</b> at <b>{escape(terms.get('place', ''))}</b> between:", styles["base"]),
        Spacer(1, 4),
        Paragraph(f"<b>{escape(company['name'])}</b>, having its registered office at {escape(company.get('address', ''))} (the \"<b>Company</b>\"); and", styles["party"]),
        Paragraph(f"<b>{escape(vendor['legal_name'])}</b>, a {escape(vendor.get('type', '') or 'vendor')} having its registered office at "
                  f"{escape(vendor.get('address', ''))}, PAN <b>{escape(vendor.get('pan', ''))}</b>"
                  f"{', GSTIN <b>' + escape(vendor['gstin']) + '</b>' if vendor.get('gstin') else ''} (the \"<b>Vendor</b>\").", styles["party"]),
        Paragraph("The Company and the Vendor are each a \"Party\" and together the \"Parties\".", styles["base"]),
        Spacer(1, 8),
    ]
    for clause in terms["clauses"]:
        body = escape(clause["body"])
        if clause["title"].strip().lower() == "term":
            body = _bold_dates(body, terms)
        story.append(Paragraph(f"<b>{clause['no']}. {escape(clause['title'])}.</b> {body}", styles["clause"]))

    signed_line = None
    vendor_name = vendor_designation = ""
    if vendor_signature_png and certificate:
        vendor_name, vendor_designation = certificate.get("signer_name", ""), certificate.get("signer_designation", "")
        signed_line = f"Signed electronically on {certificate.get('signed_at_text', '')}"
    block = _signing_block(company, vendor, terms, company_signature_png, vendor_signature_png, vendor_name, vendor_designation, signed_line, styles)
    story += [Spacer(1, 6), KeepTogether([Paragraph("<b>Signed by the Parties on the date first written above.</b>", styles["base"]), Spacer(1, 8), block])]

    if certificate:
        story += [PageBreak(), *_certificate(certificate, terms, document_sha256 or "", verify_url, styles)]

    doc.build(story, canvasmaker=Canvas)
    return buffer.getvalue()
