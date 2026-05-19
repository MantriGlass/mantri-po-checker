from flask import Flask, request, send_file, render_template, jsonify
import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib import colors
import io, re, os, tempfile

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB max

MM2IN    = 0.0393701
SQM2SQFT = 10.7639
TOLERANCE= 0.10
AREA_TOL = 0.005

RED   = colors.Color(0.75, 0.1, 0.1)
GREEN = colors.Color(0.1, 0.55, 0.2)
WHITE = colors.white
LTRED = colors.Color(1.0, 0.92, 0.92, alpha=0.92)

def nearest_x3(inches):
    lower = int(inches / 3) * 3
    if lower == 0: lower = 3
    return lower if (inches - lower) <= TOLERANCE else lower + 3

def verify_item(r):
    issues = []
    calc_w = round(r['actual_w_mm'] * MM2IN, 3)
    calc_h = round(r['actual_h_mm'] * MM2IN, 3)
    act_w_ok = abs(calc_w - r['actual_w_in']) <= TOLERANCE
    act_h_ok = abs(calc_h - r['actual_h_in']) <= TOLERANCE
    if not act_w_ok: issues.append(f"Actual W: {r['actual_w_in']}\" ≠ calc {calc_w}\"")
    if not act_h_ok: issues.append(f"Actual H: {r['actual_h_in']}\" ≠ calc {calc_h}\"")

    ch_w_in = round(r['charge_w_mm'] * MM2IN, 3)
    ch_h_in = round(r['charge_h_mm'] * MM2IN, 3)
    exp_w   = nearest_x3(ch_w_in)
    exp_h   = nearest_x3(ch_h_in)
    ch_w_ok = abs(ch_w_in - exp_w) <= TOLERANCE
    ch_h_ok = abs(ch_h_in - exp_h) <= TOLERANCE
    corr_ch_w = r['charge_w_mm'] if ch_w_ok else round(exp_w / MM2IN)
    corr_ch_h = r['charge_h_mm'] if ch_h_ok else round(exp_h / MM2IN)
    if not ch_w_ok: issues.append(f"Charge W: {r['charge_w_mm']}mm → must be {corr_ch_w}mm ({exp_w}\")")
    if not ch_h_ok: issues.append(f"Charge H: {r['charge_h_mm']}mm → must be {corr_ch_h}mm ({exp_h}\")")

    po_area   = round((r['charge_w_mm']/1000)*(r['charge_h_mm']/1000)*r['qty'], 4)
    corr_area = round((corr_ch_w/1000)*(corr_ch_h/1000)*r['qty'], 4)
    area_ok   = abs(po_area - r['area_sqm']) <= AREA_TOL
    if not area_ok: issues.append(f"Area: {r['area_sqm']} m² ≠ calc {po_area} m²")

    return {**r,
            'issues': issues, 'ok': len(issues)==0,
            'calc_w': calc_w, 'calc_h': calc_h,
            'ch_w_in': ch_w_in, 'ch_h_in': ch_h_in,
            'exp_w': exp_w, 'exp_h': exp_h,
            'corr_ch_w': corr_ch_w, 'corr_ch_h': corr_ch_h,
            'corr_area': corr_area,
            'corr_amount': round(corr_area * r['price'], 2),
            'area_ok': area_ok, 'ch_w_ok': ch_w_ok, 'ch_h_ok': ch_h_ok,
            'act_w_ok': act_w_ok, 'act_h_ok': act_h_ok}

def extract_data(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    def f(pat, default=""):
        m = re.search(pat, text)
        return m.group(1).replace(',','').strip() if m else default

    items = []
    for m in re.finditer(
        r'(\d+)\s+(\d+)\s+T-BLOCK\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
        text):
        items.append({
            'sno': int(m.group(1)), 'ref': int(m.group(2)), 'type': 'T-BLOCK',
            'actual_w_in': float(m.group(3)), 'actual_h_in': float(m.group(4)),
            'actual_w_mm': int(m.group(5)),   'actual_h_mm': int(m.group(6)),
            'charge_w_mm': int(m.group(7)),   'charge_h_mm': int(m.group(8)),
            'qty': int(m.group(9)),
            'area_sqm': float(m.group(10)),   'price': float(m.group(11)),
            'amount': float(m.group(12)),
        })

    return {
        'items': items,
        'proforma_no': f(r'Proforma No:\s*([\w/-]+)'),
        'date': f(r'Date\s*:\s*(\d{2}-\d{2}-\d{4})'),
        'subtotal':   float(f(r'Subtotal\s+Rs\.\s+([\d,.]+)', '0')),
        'ins_pct':    float(f(r'Insurance Charges\s+(\d+)\s+%', '0')),
        'add_chg':    float(f(r'Additonal ChargeS\s+\d+\s+Rs\.\s+([\d,.]+)', '0')),
        'grand_sub':  float(f(r'Grand SubTotal\s+([\d,.]+)', '0')),
        'sgst':       float(f(r'SGST\s*\(\d+\s*%\s*\)\s+([\d,.]+)', '0')),
        'cgst':       float(f(r'CGST\s*\(\d+\s*%\s*\)\s+([\d,.]+)', '0')),
        'roundoff':   float(f(r'RoundOf\s+([\d,.]+)', '0')),
        'grand_total':float(f(r'Grand Total\s+([\d,.]+)', '0')),
        'total_sqmt': float(f(r'Sq\.Mt\s+([\d.]+)', '0')),
        'total_rs':   float(f(r'Rs\.\s+([\d,]+\.\d+)', '0')),
    }

def build_overlay(verified, data, page_h):
    packet = io.BytesIO()
    c = rl_canvas.Canvas(packet, pagesize=(595, page_h))

    def py(top): return page_h - top

    row_tops = {1:339.66, 2:354.16, 3:368.66, 4:383.16, 5:397.66, 6:412.16}
    ROW_H = 14.5

    COL = {
        'ch_w_mm':  (264, 292),
        'ch_h_mm':  (292, 320),
        'area_sqm': (455, 485),
        'amount':   (540, 578),
    }

    def white_and_write(c, x0, x1, row_top, new_val, align='c'):
        row_pdf_y  = py(row_top)
        text_pdf_y = row_pdf_y - ROW_H + 3.5
        c.setFillColor(WHITE); c.setStrokeColor(WHITE)
        c.rect(x0, row_pdf_y - ROW_H + 1, x1-x0, ROW_H-1.5, fill=1, stroke=0)
        c.setFillColor(RED); c.setFont('Helvetica-Bold', 6.5)
        if align == 'c':
            c.drawCentredString((x0+x1)/2, text_pdf_y, str(new_val))
        else:
            c.drawRightString(x1-1, text_pdf_y, str(new_val))

    for v in verified:
        if v['sno'] not in row_tops: continue
        rt = row_tops[v['sno']]

        tick_y = py(rt) - ROW_H + 3.5
        if v['ok']:
            c.setFillColor(GREEN); c.setFont('Helvetica-Bold', 6)
            c.drawCentredString(587, tick_y, '✓')
        else:
            c.setFillColor(LTRED); c.setStrokeColor(colors.Color(0.8,0.3,0.3,alpha=0.3))
            c.rect(14, py(rt)-ROW_H+1, 568, ROW_H-1.5, fill=1, stroke=1)
            c.setFillColor(RED); c.setFont('Helvetica-Bold', 6)
            c.drawCentredString(587, tick_y, '✗')

        if not v['ch_w_ok']:
            white_and_write(c, *COL['ch_w_mm'], rt, v['corr_ch_w'])
        if not v['ch_h_ok']:
            white_and_write(c, *COL['ch_h_mm'], rt, v['corr_ch_h'])
        if not v['area_ok']:
            white_and_write(c, *COL['area_sqm'], rt, f"{v['corr_area']:.3f}")
        if v['corr_amount'] != v['amount']:
            white_and_write(c, *COL['amount'], rt, f"{v['corr_amount']:.2f}", align='r')

    # Financials
    total_rs   = round(sum(v['corr_amount'] for v in verified), 2)
    new_sub    = total_rs
    new_ins    = round(new_sub * data['ins_pct'] / 100, 2)
    new_add    = data['add_chg']
    new_gsub   = round(new_sub + new_ins + new_add, 2)
    gst_r      = round(data['sgst'] / data['grand_sub'] * 100) if data['grand_sub'] else 9
    new_sgst   = round(new_gsub * gst_r / 100, 2)
    new_cgst   = round(new_gsub * gst_r / 100, 2)
    new_grand  = new_gsub + new_sgst + new_cgst
    new_roff   = round(round(new_grand) - new_grand, 2)
    new_gtotal = round(new_grand + new_roff, 2)

    fin_updates = [
        (462.9, 538, 578, data['subtotal'],    new_sub),
        (502.8, 530, 578, data['grand_sub'],   new_gsub),
        (515.4, 534, 578, data['sgst'],        new_sgst),
        (528.4, 534, 578, data['cgst'],        new_cgst),
        (540.9, 548, 578, data['roundoff'],    new_roff),
        (556.3, 528, 578, data['grand_total'], new_gtotal),
    ]
    for (top, x0, x1, old, new) in fin_updates:
        if old is None or abs(float(old or 0) - new) > 0.005:
            fpdf_y = py(top)
            c.setFillColor(WHITE); c.setStrokeColor(WHITE)
            c.rect(x0, fpdf_y-9, x1-x0, 9, fill=1, stroke=0)
            c.setFillColor(colors.HexColor("#0f2044"))
            c.setFont('Helvetica-Bold' if top > 554 else 'Helvetica', 6.5)
            c.drawRightString(x1, fpdf_y-1.5, f"{new:,.2f}")

    err_count = sum(1 for v in verified if not v['ok'])
    if err_count > 0:
        banner_top = py(436)
        c.setFillColor(colors.Color(1.0,0.9,0.9,alpha=0.95))
        c.setStrokeColor(RED)
        c.rect(14, banner_top-10, 568, 10, fill=1, stroke=1)
        c.setFillColor(RED); c.setFont('Helvetica-Bold', 6.5)
        c.drawCentredString(297, banner_top-7, f"⚠  {err_count} ROW(S) HAVE ERRORS — see red highlights")
        issues_y = banner_top - 12
        for v in verified:
            for iss in v['issues']:
                issues_y -= 8
                c.setFillColor(RED); c.setFont('Helvetica', 6)
                c.drawString(20, issues_y, f"SR {v['sno']}: {iss}")

    c.save()
    packet.seek(0)
    return packet

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/verify', methods=['POST'])
def verify():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['pdf']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Please upload a PDF file'}), 400

    pdf_bytes = f.read()

    try:
        data = extract_data(pdf_bytes)
    except Exception as e:
        return jsonify({'error': f'Could not read PDF: {str(e)}'}), 400

    if not data['items']:
        return jsonify({'error': 'No line items found in this PDF. Make sure it is a Fancy Glass PO.'}), 400

    verified = [verify_item(r) for r in data['items']]
    err_count = sum(1 for v in verified if not v['ok'])

    # Build overlay
    reader    = PdfReader(io.BytesIO(pdf_bytes))
    orig_page = reader.pages[0]
    page_h    = float(orig_page.mediabox.height)

    overlay_pdf  = build_overlay(verified, data, page_h)
    overlay_page = PdfReader(overlay_pdf).pages[0]
    orig_page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(orig_page)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)

    # Summary for UI
    summary = {
        'total': len(verified),
        'passed': len(verified) - err_count,
        'errors': err_count,
        'proforma_no': data['proforma_no'],
        'date': data['date'],
        'rows': [{
            'sno': v['sno'],
            'ok': v['ok'],
            'issues': v['issues'],
            'corr_area': v['corr_area'],
            'corr_amount': v['corr_amount'],
        } for v in verified],
        'filename': f.filename.replace('.pdf', '_VERIFIED.pdf'),
    }

    # Return PDF with summary in header
    response = send_file(
        out_buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=summary['filename']
    )
    response.headers['X-Summary'] = str(summary).replace('\n','')
    return response

@app.route('/check', methods=['POST'])
def check_only():
    """Returns JSON summary without generating PDF — for live preview"""
    if 'pdf' not in request.files:
        return jsonify({'error': 'No file'}), 400
    pdf_bytes = request.files['pdf'].read()
    try:
        data    = extract_data(pdf_bytes)
        verified= [verify_item(r) for r in data['items']]
        err_count = sum(1 for v in verified if not v['ok'])
        return jsonify({
            'ok': True,
            'proforma_no': data['proforma_no'],
            'date': data['date'],
            'total': len(verified),
            'passed': len(verified) - err_count,
            'errors': err_count,
            'rows': [{
                'sno': v['sno'], 'type': v['type'],
                'ok': v['ok'], 'issues': v['issues'],
                'actual_w_in': v['actual_w_in'], 'actual_h_in': v['actual_h_in'],
                'actual_w_mm': v['actual_w_mm'], 'actual_h_mm': v['actual_h_mm'],
                'charge_w_mm': v['charge_w_mm'], 'charge_h_mm': v['charge_h_mm'],
                'corr_ch_w': v['corr_ch_w'], 'corr_ch_h': v['corr_ch_h'],
                'area_sqm': v['area_sqm'], 'corr_area': v['corr_area'],
                'qty': v['qty'], 'price': v['price'],
                'amount': v['amount'], 'corr_amount': v['corr_amount'],
                'ch_w_ok': v['ch_w_ok'], 'ch_h_ok': v['ch_h_ok'],
                'area_ok': v['area_ok'], 'act_w_ok': v['act_w_ok'], 'act_h_ok': v['act_h_ok'],
            } for v in verified],
            'total_area': round(sum(v['corr_area'] for v in verified), 3),
            'total_amount': round(sum(v['corr_amount'] for v in verified), 2),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
