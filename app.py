from flask import Flask, request, send_file, render_template, jsonify
import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib import colors
import io, re, os

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

MM2IN    = 0.0393701
SQM2SQFT = 10.7639
TOLERANCE= 0.10
AREA_TOL = 0.005

RED   = colors.Color(0.75, 0.1,  0.1)
GREEN = colors.Color(0.1,  0.55, 0.2)
WHITE = colors.white
LTRED = colors.Color(1.0,  0.92, 0.92, alpha=0.92)

# ══════════════════════════════════════════════════════════════════
#  FRACTION PARSER  —  handles "70 7/8", "109 2/8", "35 7/16", "48"
# ══════════════════════════════════════════════════════════════════
def parse_inch(val):
    if val is None: return None
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    # "70 7/8"  or  "109 2/8"  or  "35 7/16"
    m = re.match(r'^(\d+)\s+(\d+)/(\d+)$', s)
    if m: return int(m.group(1)) + int(m.group(2))/int(m.group(3))
    # plain fraction "7/8"
    m = re.match(r'^(\d+)/(\d+)$', s)
    if m: return int(m.group(1))/int(m.group(2))
    # plain number
    try: return float(s)
    except: return None

def nearest_x3(inches):
    lower = int(inches / 3) * 3
    if lower == 0: lower = 3
    return lower if (inches - lower) <= TOLERANCE else lower + 3

# ══════════════════════════════════════════════════════════════════
#  UNIVERSAL LINE-ITEM EXTRACTOR
# ══════════════════════════════════════════════════════════════════
def extract_items(text):
    """
    Detects which format the PDF is and extracts line items.
    Returns list of dicts with standard keys.
    """
    items = []

    # ── FORMAT 1: Fancy Glass  (T-BLOCK / T-DRAWING)
    # Pattern: sno ref TYPE  w_in h_in  w_mm h_mm  cw_mm ch_mm  qty  area  price  amount
    # e.g.: 1 1 T-BLOCK 54.7 66 1390 1677 1448 1677 2 4.854 620.00 3009.48
    # e.g.: 1 1 T-DRAWING 70 7/8 35 7/16 1800 900 1829 915 1 1 1.672 1300.00 2173.60
    fancy_pat = re.compile(
        r'(\d+)\s+(\d+)\s+(T-BLOCK|T-DRAWING|T-\w+)'   # sno ref type
        r'\s+([\d./]+(?:\s+\d+/\d+)?)'                  # w_in (may have fraction)
        r'\s+([\d./]+(?:\s+\d+/\d+)?)'                  # h_in
        r'\s+(\d+)\s+(\d+)'                             # w_mm h_mm
        r'\s+(\d+)\s+(\d+)'                             # cw_mm ch_mm
        r'\s+(\d+)'                                     # qty
        r'(?:\s+\d+)*'                                  # optional process cols (H C SP BH CSK)
        r'\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)'            # area price amount
    )
    for m in fancy_pat.finditer(text):
        items.append({
            'sno':          int(m.group(1)),
            'type':         m.group(3),
            'actual_w_in':  parse_inch(m.group(4)),
            'actual_h_in':  parse_inch(m.group(5)),
            'actual_w_mm':  int(m.group(6)),
            'actual_h_mm':  int(m.group(7)),
            'charge_w_mm':  int(m.group(8)),
            'charge_h_mm':  int(m.group(9)),
            'qty':          int(m.group(10)),
            'area_sqm':     float(m.group(11)),
            'price':        float(m.group(12)),
            'amount':       float(m.group(13)),
        })

    if items:
        return items, 'fancy'

    # ── FORMAT 2: Glass Build Industry  (DRAWING / BLOCK)
    # Pattern: sno DRAWING  w_in h_in  w_mm h_mm  cw_mm ch_mm  qty
    #          then optional process cols then area  price  amount
    # e.g.: 1 DRAWING 48 109 2/8 1218 2775 1219 2819 3 10.311 1625.00 16755.38
    # e.g.: 2 DRAWING 48 109 2/8 1218 2775 1219 2819 1 2 1 3.437 1625.00 5585.12
    # Tricky: process cols (H C CSK BH BC) can have 0-6 numbers before area

    # Split text into lines and parse line by line
    for line in text.split('\n'):
        line = line.strip()
        # Must start with a digit (SR no)
        if not re.match(r'^\d+\s+(DRAWING|BLOCK|T-BLOCK|T-DRAWING)', line):
            continue

        # Try to tokenize the line
        tokens = line.split()
        if len(tokens) < 10:
            continue

        try:
            sno  = int(tokens[0])
            typ  = tokens[1]
            idx  = 2

            # Read W_IN — may be "48" or "71 7/8" (two tokens)
            w_in_str = tokens[idx]; idx += 1
            if idx < len(tokens) and re.match(r'^\d+/\d+$', tokens[idx]):
                w_in_str += ' ' + tokens[idx]; idx += 1

            # Read H_IN — same logic
            h_in_str = tokens[idx]; idx += 1
            if idx < len(tokens) and re.match(r'^\d+/\d+$', tokens[idx]):
                h_in_str += ' ' + tokens[idx]; idx += 1

            w_in = parse_inch(w_in_str)
            h_in = parse_inch(h_in_str)
            if w_in is None or h_in is None: continue

            # Next 4 are w_mm h_mm cw_mm ch_mm
            w_mm  = int(tokens[idx]);   idx += 1
            h_mm  = int(tokens[idx]);   idx += 1
            cw_mm = int(tokens[idx]);   idx += 1
            ch_mm = int(tokens[idx]);   idx += 1

            # Remaining tokens before the last 3 (area price amount)
            # are qty + process cols — take the first numeric as qty
            qty = int(tokens[idx]); idx += 1

            # Skip process columns (single/double digit numbers) until we hit area (has decimal)
            while idx < len(tokens) - 3:
                t = tokens[idx]
                if '.' in t:
                    break  # this is the area
                # it's a process col number, skip
                idx += 1

            area   = float(tokens[idx]);   idx += 1
            price  = float(tokens[idx]);   idx += 1
            amount = float(tokens[idx]);   idx += 1

            items.append({
                'sno': sno, 'type': typ,
                'actual_w_in': w_in,   'actual_h_in': h_in,
                'actual_w_mm': w_mm,   'actual_h_mm': h_mm,
                'charge_w_mm': cw_mm,  'charge_h_mm': ch_mm,
                'qty': qty,
                'area_sqm': area,  'price': price,  'amount': amount,
            })
        except (IndexError, ValueError):
            continue

    return items, 'glassbuild' if items else 'unknown'


# ══════════════════════════════════════════════════════════════════
#  EXTRACT FULL DATA FROM PDF
# ══════════════════════════════════════════════════════════════════
def extract_data(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    def f(pat, default=""):
        m = re.search(pat, text)
        return m.group(1).replace(',','').strip() if m else default

    items, fmt = extract_items(text)

    return {
        'items':  items,
        'format': fmt,
        'proforma_no': (f(r'Proforma No[:\s]+([\w/-]+)') or
                        f(r'Proforma No\s*:\s*([\w/-]+)')),
        'date':        (f(r'Date\s*:\s*(\d{2}[-/]\d{2}[-/]\d{4})')),
        'subtotal':    float(f(r'Subtotal\s*(?:Rs\.)?\s*([\d,.]+)', '0')),
        'ins_pct':     float(f(r'Insurance(?:\s+Charges?)?\s+([\d.]+)\s*%', '0')),
        'add_chg':     float(f(r'(?:Additonal|Additional)\s+Charge[sS]\s+\d+\s+Rs\.\s+([\d,.]+)', '0')),
        'grand_sub':   float(f(r'Grand\s+SubTotal\s+([\d,.]+)', '0')),
        'sgst':        float(f(r'S(?:-)?GST\s*(?:\(\d+\s*%\s*\))?\s*(?:\d+\.\d+\s*%)?\s*([\d,.]+)', '0')),
        'cgst':        float(f(r'C(?:-)?GST\s*(?:\(\d+\s*%\s*\))?\s*(?:\d+\.\d+\s*%)?\s*([\d,.]+)', '0')),
        'roundoff':    float(f(r'Round\s*Of+\s+([\d,.]+)', '0')),
        'grand_total': float(f(r'Grand\s+Total\s+([\d,.]+)', '0')),
        'total_sqmt':  float(f(r'Sq\.Mt\s+([\d.]+)', '0')),
        'total_rs':    float(f(r'Rs\.\s+([\d,]+\.\d+)', '0')),
    }


# ══════════════════════════════════════════════════════════════════
#  VERIFY ONE ITEM
# ══════════════════════════════════════════════════════════════════
def verify_item(r):
    issues = []

    calc_w = round(r['actual_w_mm'] * MM2IN, 3)
    calc_h = round(r['actual_h_mm'] * MM2IN, 3)
    act_w_ok = r['actual_w_in'] is None or abs(calc_w - r['actual_w_in']) <= TOLERANCE
    act_h_ok = r['actual_h_in'] is None or abs(calc_h - r['actual_h_in']) <= TOLERANCE
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
            'exp_w': exp_w,   'exp_h': exp_h,
            'corr_ch_w': corr_ch_w, 'corr_ch_h': corr_ch_h,
            'corr_area': corr_area,
            'corr_amount': round(corr_area * r['price'], 2),
            'area_ok': area_ok, 'ch_w_ok': ch_w_ok, 'ch_h_ok': ch_h_ok,
            'act_w_ok': act_w_ok, 'act_h_ok': act_h_ok}


# ══════════════════════════════════════════════════════════════════
#  BUILD OVERLAY PDF  —  works for BOTH formats
#  Strategy: place tick/cross + red highlights on errors
#  Uses relative row detection so it works on any layout
# ══════════════════════════════════════════════════════════════════
def build_overlay(verified, data, pdf_bytes, page_h):
    # Detect row y-positions dynamically from PDF
    row_tops = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page  = pdf.pages[0]
            words = page.extract_words()
            # Find each SR number's y position
            for v in verified:
                sno_str = str(v['sno'])
                for w in words:
                    if (w['text'] == sno_str and
                        float(w['x0']) < 35 and
                        float(w['top']) > 200):
                        row_tops[v['sno']] = float(w['top'])
                        break

            # Detect column x positions dynamically
            # Find charge W column by looking for charge_w_mm value in table area
            col_charge_w = None
            col_charge_h = None
            col_area     = None
            col_amount   = None

            for v in verified:
                if v['sno'] not in row_tops: continue
                rt = row_tops[v['sno']]
                row_words = [w for w in words if abs(float(w['top']) - rt) < 8]
                for w in row_words:
                    t = w['text'].replace(',','')
                    try:
                        val = float(t)
                        xc  = (float(w['x0']) + float(w['x1'])) / 2
                        # Match charge_w_mm
                        if abs(val - v['charge_w_mm']) < 2 and col_charge_w is None:
                            col_charge_w = (float(w['x0']), float(w['x1']))
                        # Match charge_h_mm
                        elif abs(val - v['charge_h_mm']) < 2 and col_charge_h is None and col_charge_w and xc > (col_charge_w[0]+col_charge_w[1])/2:
                            col_charge_h = (float(w['x0']), float(w['x1']))
                        # Match area
                        elif abs(val - v['area_sqm']) < 0.01 and col_area is None:
                            col_area = (float(w['x0']), float(w['x1']))
                        # Match amount
                        elif abs(val - v['amount']) < 0.05 and col_amount is None:
                            col_amount = (float(w['x0']), float(w['x1']))
                    except: pass
                if col_charge_w and col_charge_h and col_area and col_amount:
                    break
    except Exception as e:
        print(f"Column detection error: {e}")

    # Fallback to Fancy Glass defaults if detection failed
    if not col_charge_w: col_charge_w = (264, 292)
    if not col_charge_h: col_charge_h = (292, 320)
    if not col_area:     col_area     = (455, 485)
    if not col_amount:   col_amount   = (540, 578)

    ROW_H = 15.0

    packet = io.BytesIO()
    c = rl_canvas.Canvas(packet, pagesize=(595, page_h))

    def py(top): return page_h - top

    def white_write(x0, x1, row_top, new_val, align='c'):
        rpy  = py(row_top)
        ty   = rpy - ROW_H + 4
        c.setFillColor(WHITE); c.setStrokeColor(WHITE)
        c.rect(x0, rpy - ROW_H + 1, x1-x0, ROW_H-1.5, fill=1, stroke=0)
        c.setFillColor(RED); c.setFont('Helvetica-Bold', 6.5)
        if align == 'c':
            c.drawCentredString((x0+x1)/2, ty, str(new_val))
        else:
            c.drawRightString(x1-1, ty, str(new_val))

    for v in verified:
        if v['sno'] not in row_tops:
            continue
        rt = row_tops[v['sno']]

        # Tick/cross at far right
        tick_y = py(rt) - ROW_H + 4
        if v['ok']:
            c.setFillColor(GREEN); c.setFont('Helvetica-Bold', 7)
            c.drawString(580, tick_y, '✓')
        else:
            # Red background on error row
            c.setFillColor(LTRED); c.setStrokeColor(colors.Color(0.8,0.3,0.3,alpha=0.3))
            c.rect(14, py(rt)-ROW_H+1, 568, ROW_H-1.5, fill=1, stroke=1)
            c.setFillColor(RED); c.setFont('Helvetica-Bold', 7)
            c.drawString(580, tick_y, '✗')

        if not v['ch_w_ok']:
            white_write(*col_charge_w, rt, v['corr_ch_w'])
        if not v['ch_h_ok']:
            white_write(*col_charge_h, rt, v['corr_ch_h'])
        if not v['area_ok']:
            white_write(*col_area, rt, f"{v['corr_area']:.3f}")
        if abs(v['corr_amount'] - v['amount']) > 0.01:
            white_write(*col_amount, rt, f"{v['corr_amount']:.2f}", align='r')

    # Error banner if needed
    err_count = sum(1 for v in verified if not v['ok'])
    if err_count > 0 and row_tops:
        last_row_top = max(row_tops.values())
        banner_y = py(last_row_top + 22)
        c.setFillColor(colors.Color(1.0,0.9,0.9,alpha=0.95))
        c.setStrokeColor(RED)
        c.rect(14, banner_y-10, 568, 10, fill=1, stroke=1)
        c.setFillColor(RED); c.setFont('Helvetica-Bold', 6.5)
        c.drawCentredString(297, banner_y-7,
            f"⚠  {err_count} ROW(S) HAVE ERRORS — corrected values shown in red")
        iy = banner_y - 12
        for v in verified:
            for iss in v['issues']:
                iy -= 8
                c.setFillColor(RED); c.setFont('Helvetica', 6)
                c.drawString(20, iy, f"SR {v['sno']}: {iss}")

    c.save()
    packet.seek(0)
    return packet


# ══════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check', methods=['POST'])
def check_only():
    if 'pdf' not in request.files:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    pdf_bytes = request.files['pdf'].read()
    try:
        data     = extract_data(pdf_bytes)
        if not data['items']:
            return jsonify({'ok': False, 'error': 'No line items found. Make sure this is a glass PO PDF.'}), 400
        verified = [verify_item(r) for r in data['items']]
        err_count= sum(1 for v in verified if not v['ok'])
        return jsonify({
            'ok': True,
            'format': data['format'],
            'proforma_no': data['proforma_no'],
            'date': data['date'],
            'total':  len(verified),
            'passed': len(verified) - err_count,
            'errors': err_count,
            'total_area':   round(sum(v['corr_area']   for v in verified), 3),
            'total_amount': round(sum(v['corr_amount']  for v in verified), 2),
            'rows': [{
                'sno': v['sno'], 'type': v['type'], 'qty': v['qty'],
                'ok': v['ok'],   'issues': v['issues'],
                'actual_w_in': v['actual_w_in'], 'actual_h_in': v['actual_h_in'],
                'actual_w_mm': v['actual_w_mm'], 'actual_h_mm': v['actual_h_mm'],
                'charge_w_mm': v['charge_w_mm'], 'charge_h_mm': v['charge_h_mm'],
                'corr_ch_w':   v['corr_ch_w'],   'corr_ch_h':   v['corr_ch_h'],
                'area_sqm':    v['area_sqm'],     'corr_area':   v['corr_area'],
                'price':       v['price'],        'amount':      v['amount'],
                'corr_amount': v['corr_amount'],
                'ch_w_ok':  v['ch_w_ok'],  'ch_h_ok':  v['ch_h_ok'],
                'area_ok':  v['area_ok'],  'act_w_ok': v['act_w_ok'],
                'act_h_ok': v['act_h_ok'],
            } for v in verified],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/verify', methods=['POST'])
def verify():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['pdf']
    pdf_bytes = f.read()
    try:
        data     = extract_data(pdf_bytes)
        if not data['items']:
            return jsonify({'error': 'No line items found'}), 400
        verified = [verify_item(r) for r in data['items']]

        reader    = PdfReader(io.BytesIO(pdf_bytes))
        orig_page = reader.pages[0]
        page_h    = float(orig_page.mediabox.height)

        overlay      = build_overlay(verified, data, pdf_bytes, page_h)
        overlay_page = PdfReader(overlay).pages[0]
        orig_page.merge_page(overlay_page)

        writer = PdfWriter()
        writer.add_page(orig_page)
        out_buf = io.BytesIO()
        writer.write(out_buf)
        out_buf.seek(0)

        dl_name = f.filename.replace('.pdf','') + '_VERIFIED.pdf'
        return send_file(out_buf, mimetype='application/pdf',
                         as_attachment=True, download_name=dl_name)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
