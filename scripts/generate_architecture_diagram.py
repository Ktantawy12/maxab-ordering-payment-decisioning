#!/usr/bin/env python
"""Renders docs/architecture.png -- a one-page landscape diagram of the
actual implemented pipeline (infrastructure/template.yaml + src/**). Not
part of the pipeline itself; a documentation-generation script, run once
and the output committed.

Every element shown here was verified against the live template.yaml and
handler source, not assumed. Color = component category; line style =
relationship type (see the on-image legend). Box sizes are measured from
their own text content (not guessed), so nothing overflows or overlaps.

Requires Pillow, which is intentionally NOT in requirements.txt (that file
is application/runtime + test dependencies only). Install from
requirements-dev.txt to regenerate this diagram:
    pip install -r requirements-dev.txt
    python scripts/generate_architecture_diagram.py
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "C:/Windows/Fonts/"


def font(name, size):
    return ImageFont.truetype(FONT_DIR + name, size)


F_TITLE = font("arialbd.ttf", 36)
F_SUB = font("arial.ttf", 19)
F_HEAD = font("arialbd.ttf", 18)
F_BODY = font("arial.ttf", 15)
F_TINY = font("arial.ttf", 13)
F_LABEL = font("arialbd.ttf", 14)
F_LEGEND = font("arial.ttf", 16)
F_CAPTION = font("arialbd.ttf", 19)

# --- palette -------------------------------------------------------------
BG = "#FFFFFF"
INK = "#1A1A1A"
MUTED = "#5A5A5A"

STORE_FILL, STORE_LINE = "#E8F5E9", "#2E7D32"
COMPUTE_FILL, COMPUTE_LINE = "#E3F2FD", "#1565C0"
EVENT_FILL, EVENT_LINE = "#FFF3E0", "#E65100"
EXTERNAL_FILL, EXTERNAL_LINE = "#F5F5F5", "#616161"
LOGS_FILL, LOGS_LINE = "#F3E5F5", "#6A1B9A"

DATA_COLOR = "#37474F"
TRIGGER_COLOR = "#E65100"
FAIL_COLOR = "#C62828"
READ_COLOR = "#1565C0"
WRITE_COLOR = "#2E7D32"

PAD_X, PAD_Y, LINE_GAP, HEAD_GAP = 18, 14, 6, 10

# canvas sized after layout is computed; start with a generous guess
W, H = 2300, 1500
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def measure(lines, f):
    w = 0
    h = 0
    for i, line in enumerate(lines):
        bbox = d.textbbox((0, 0), line, font=f)
        w = max(w, bbox[2] - bbox[0])
        h += (bbox[3] - bbox[1]) + (LINE_GAP if i else 0)
    return w, h


def component_size(title, sublines, badge_rows=0):
    tw, th = measure([title], F_HEAD)
    bw, bh = measure(sublines, F_BODY) if sublines else (0, 0)
    width = max(tw, bw) + 2 * PAD_X
    height = PAD_Y + th + HEAD_GAP + 6 + (PAD_Y // 2) + bh + PAD_Y
    if badge_rows:
        height += badge_rows * 40
    return int(width), int(height)


def text_center(xy, s, f, fill=INK):
    x, y = xy
    bbox = d.textbbox((0, 0), s, font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((x - w / 2, y - h / 2 - bbox[1]), s, font=f, fill=fill)


def text_lines_center(cx, top_y, lines, f, fill=INK):
    y = top_y
    for line in lines:
        bbox = d.textbbox((0, 0), line, font=f)
        h = bbox[3] - bbox[1]
        text_center((cx, y + h / 2), line, f, fill)
        y += h + LINE_GAP
    return y


def box(xy, fill, line, radius=10, width=2):
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=line, width=width)


def component(xy, title, sublines, fill, line_color, title_color=None):
    box(xy, fill, line_color)
    x0, y0, x1, y1 = xy
    cx = (x0 + x1) / 2
    y = y0 + PAD_Y
    tb = d.textbbox((0, 0), title, font=F_HEAD)
    text_center((cx, y + (tb[3] - tb[1]) / 2), title, F_HEAD, title_color or line_color)
    y += (tb[3] - tb[1]) + HEAD_GAP
    d.line([(x0 + 14, y), (x1 - 14, y)], fill=line_color, width=1)
    y += PAD_Y // 2
    text_lines_center(cx, y, sublines, F_BODY, INK)


def badges(xy_top_left_area, labels_colors, box_w_total, y):
    x0 = xy_top_left_area
    n = len(labels_colors)
    gap = 10
    bw = (box_w_total - gap * (n - 1)) / n
    x = x0
    for label, col in labels_colors:
        tb = d.textbbox((0, 0), label, font=F_TINY)
        tw = tb[2] - tb[0]
        this_w = max(bw, tw + 20)
        rect = (x, y, x + this_w, y + 32)
        box(rect, "#FFFFFF", col, radius=6, width=2)
        text_center(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), label, F_TINY, col)
        x += this_w + gap
    return x - gap  # right edge actually used


def arrowhead(p1, ang, color):
    size = 12
    a1 = ang + math.radians(150)
    a2 = ang - math.radians(150)
    p_a = (p1[0] + size * math.cos(a1), p1[1] + size * math.sin(a1))
    p_b = (p1[0] + size * math.cos(a2), p1[1] + size * math.sin(a2))
    d.polygon([p1, p_a, p_b], fill=color)


def polyline_arrow(points, color, width=3, dashed=False, dash=9, gap=6):
    for i in range(len(points) - 1):
        p0, p1 = points[i], points[i + 1]
        _seg(p0, p1, color, width, dashed, dash, gap)
    ang = math.atan2(points[-1][1] - points[-2][1], points[-1][0] - points[-2][0])
    arrowhead(points[-1], ang, color)


def _seg(p0, p1, color, width, dashed, dash, gap):
    x0, y0 = p0
    x1, y1 = p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return
    if dashed:
        n = int(length // (dash + gap)) + 1
        for i in range(n):
            s = i * (dash + gap)
            e = min(s + dash, length)
            if s >= length:
                break
            sx, sy = x0 + (x1 - x0) * s / length, y0 + (y1 - y0) * s / length
            ex, ey = x0 + (x1 - x0) * e / length, y0 + (y1 - y0) * e / length
            d.line([(sx, sy), (ex, ey)], fill=color, width=width)
    else:
        d.line([p0, p1], fill=color, width=width)


def label_on_line(mid, text, color, bg=BG):
    bbox = d.textbbox((0, 0), text, font=F_LABEL)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 5
    x, y = mid
    d.rectangle([x - w / 2 - pad, y - h / 2 - pad, x + w / 2 + pad, y + h / 2 + pad], fill=bg)
    text_center((x, y), text, F_LABEL, color)


def right_mid(b):
    return (b[2], (b[1] + b[3]) / 2)


def left_mid(b):
    return (b[0], (b[1] + b[3]) / 2)


def top_mid(b):
    return ((b[0] + b[2]) / 2, b[1])


def bottom_mid(b):
    return ((b[0] + b[2]) / 2, b[3])


def top_at(b, frac):
    return (b[0] + (b[2] - b[0]) * frac, b[1])


def bottom_at(b, frac):
    return (b[0] + (b[2] - b[0]) * frac, b[3])


def left_at(b, frac):
    return (b[0], b[1] + (b[3] - b[1]) * frac)


def right_at(b, frac):
    return (b[2], b[1] + (b[3] - b[1]) * frac)


# =========================================================================
# LAYOUT -- rows top to bottom, columns left to right, all gaps generous
# =========================================================================
GAP_X = 170
ROW1_Y = 190     # Generator / S3 / EventBridge / DecisionEngine / DecisionsTable
ROW2_Y = 560     # CustomerProfilesTable
ROW3_Y = 830     # DownstreamProcessor / DownstreamActionsTable
ROW4_Y = 1140    # DLQs
ROW5_Y = 1300    # CloudWatch Logs

# --- Row 1 ---
gen_w, gen_h = component_size("Order Generator", ["scripts/seed_data.py", "100,000+ records", "40 NDJSON batches"])
gen = (60, ROW1_Y, 60 + gen_w, ROW1_Y + gen_h)

s3_w, s3_h = component_size("S3: RawOrdersBucket", ["batches/ prefix", "SSE-S3, Block Public Access",
                              "Versioning: Suspended", "Lifecycle: 30-day expiry"])
s3x = gen[2] + GAP_X
s3 = (s3x, ROW1_Y, s3x + s3_w, ROW1_Y + s3_h)

eb_w, eb_h = component_size("EventBridge (default bus)", ["Rule: OrdersUploaded", "S3 Object Created",
                              "key prefix = batches/"])
ebx = s3[2] + GAP_X
eb = (ebx, ROW1_Y - 40, ebx + eb_w, ROW1_Y - 40 + eb_h)

de_w, de_h = component_size("Lambda: decision-engine", [
    "Python 3.14 | 512MB | 300s",
    "Signals (weighted):",
    "basket_value 0.20   tenure 0.25",
    "fraud 0.35   payment_risk 0.20",
    "-> conditional PutItem",
])
dex = eb[2] + GAP_X + 20
de = (dex, ROW1_Y - 20, dex + de_w, ROW1_Y - 20 + de_h)

dt_title = "DynamoDB: DecisionsTable"
dt_sub = ["PK order_id  |  Stream: NEW_AND_OLD_IMAGES", "GSI DecisionIndex (decision, decided_at)",
          "-- ops queries only, not read by pipeline", "On-demand billing"]
dt_w, dt_h = component_size(dt_title, dt_sub, badge_rows=1)
dt_w = max(dt_w, 470)
dtx = de[2] + GAP_X
dt = (dtx, ROW1_Y - 30, dtx + dt_w, ROW1_Y - 30 + dt_h)

# --- Row 2 ---
cp_w, cp_h = component_size("DynamoDB: CustomerProfilesTable", [
    "PK customer_id  |  no stream, no GSI",
    "Owned exclusively by downstream-processor",
    "On-demand billing",
])
cpx = (de[0] + de[2]) / 2 - cp_w / 2 + 40
cp = (cpx, ROW2_Y, cpx + cp_w, ROW2_Y + cp_h)

# --- Row 3 ---
dp_w, dp_h = component_size("Lambda: downstream-processor", [
    "Python 3.14 | 256MB | 60s",
    "Trigger: DynamoDB Stream",
    "BatchSize 10, INSERT-only filter",
    "Bisect-on-error, ReportBatchItemFailures",
])
dpx = dt[0] + 40
dp = (dpx, ROW3_Y, dpx + dp_w, ROW3_Y + dp_h)

da_title = "DynamoDB: DownstreamActionsTable"
da_sub = ["PK order_id, SK action_type", "GSI StatusIndex (status, created_at)",
          "-- ops queries only, not read by pipeline", "On-demand billing"]
da_w, da_h = component_size(da_title, da_sub, badge_rows=1)
da_w = max(da_w, 520)
dax = dp[2] + GAP_X
da = (dax, ROW3_Y - 10, dax + da_w, ROW3_Y - 10 + da_h)

# --- Row 4: DLQs ---
dedlq_w, dedlq_h = component_size("SQS: DecisionEngineDLQ", ["Async-invoke OnFailure destination"])
dedlqx = de[0]
dedlq = (dedlqx, ROW4_Y, dedlqx + dedlq_w, ROW4_Y + dedlq_h)

dpdlq_w, dpdlq_h = component_size("SQS: DownstreamProcessorDLQ", ["Stream event-source-mapping OnFailure destination"])
dpdlqx = dp[2] + GAP_X - dpdlq_w if dp[2] + GAP_X - dpdlq_w > dedlq[2] + GAP_X else dedlq[2] + GAP_X
dpdlq = (dpdlqx, ROW4_Y, dpdlqx + dpdlq_w, ROW4_Y + dpdlq_h)

# --- Row 5: CloudWatch Logs ---
cw_w, cw_h = component_size("CloudWatch Logs", ["2 Log Groups, RetentionInDays: 14", "structured JSON per invocation"])
cwx = (dedlq[0] + dpdlq[2]) / 2 - cw_w / 2
cw = (cwx, ROW5_Y, cwx + cw_w, ROW5_Y + cw_h)

# recompute canvas size from content
right_edge = max(da[2], dpdlq[2]) + 80
bottom_edge = cw[3] + 260
W = int(max(2300, right_edge))
H = int(max(1500, bottom_edge))
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# =========================================================================
# DRAW
# =========================================================================
text_center((W / 2, 40), "MaxAB Ordering & Payment Decisioning Pipeline", F_TITLE)
text_center((W / 2, 80), "Event-driven architecture -- as implemented in infrastructure/template.yaml and src/   |   region: eu-central-1",
            F_SUB, MUTED)

component(gen, "Order Generator", ["scripts/seed_data.py", "100,000+ records", "40 NDJSON batches"], EXTERNAL_FILL, EXTERNAL_LINE)
component(s3, "S3: RawOrdersBucket", ["batches/ prefix", "SSE-S3, Block Public Access",
          "Versioning: Suspended", "Lifecycle: 30-day expiry"], STORE_FILL, STORE_LINE)
component(eb, "EventBridge (default bus)", ["Rule: OrdersUploaded", "S3 Object Created", "key prefix = batches/"], EVENT_FILL, EVENT_LINE)
component(de, "Lambda: decision-engine", [
    "Python 3.14 | 512MB | 300s", "Signals (weighted):",
    "basket_value 0.20   tenure 0.25", "fraud 0.35   payment_risk 0.20", "-> conditional PutItem",
], COMPUTE_FILL, COMPUTE_LINE)

box(dt, STORE_FILL, STORE_LINE)
cx = (dt[0] + dt[2]) / 2
y = dt[1] + PAD_Y
tb = d.textbbox((0, 0), dt_title, font=F_HEAD)
text_center((cx, y + (tb[3] - tb[1]) / 2), dt_title, F_HEAD, STORE_LINE)
y += (tb[3] - tb[1]) + HEAD_GAP
d.line([(dt[0] + 14, y), (dt[2] - 14, y)], fill=STORE_LINE, width=1)
y += PAD_Y // 2
y_end = text_lines_center(cx, y, dt_sub, F_BODY, INK)
badges(dt[0] + PAD_X, [("APPROVE", "#2E7D32"), ("MANUAL_REVIEW", "#E65100"), ("DECLINE", "#C62828")],
       dt[2] - dt[0] - 2 * PAD_X, dt[3] - 44)

component(cp, "DynamoDB: CustomerProfilesTable", [
    "PK customer_id  |  no stream, no GSI", "Owned exclusively by downstream-processor", "On-demand billing",
], STORE_FILL, STORE_LINE)

component(dp, "Lambda: downstream-processor", [
    "Python 3.14 | 256MB | 60s", "Trigger: DynamoDB Stream",
    "BatchSize 10, INSERT-only filter", "Bisect-on-error, ReportBatchItemFailures",
], COMPUTE_FILL, COMPUTE_LINE)

box(da, STORE_FILL, STORE_LINE)
cx = (da[0] + da[2]) / 2
y = da[1] + PAD_Y
tb = d.textbbox((0, 0), da_title, font=F_HEAD)
text_center((cx, y + (tb[3] - tb[1]) / 2), da_title, F_HEAD, STORE_LINE)
y += (tb[3] - tb[1]) + HEAD_GAP
d.line([(da[0] + 14, y), (da[2] - 14, y)], fill=STORE_LINE, width=1)
y += PAD_Y // 2
text_lines_center(cx, y, da_sub, F_BODY, INK)
badges(da[0] + PAD_X, [("FULFILLMENT_RELEASE", "#2E7D32"), ("MANUAL_REVIEW_QUEUE", "#E65100"), ("BLOCKLIST_LOG", "#C62828")],
       da[2] - da[0] - 2 * PAD_X, da[3] - 44)

component(dedlq, "SQS: DecisionEngineDLQ", ["Async-invoke OnFailure destination"], EVENT_FILL, EVENT_LINE)
component(dpdlq, "SQS: DownstreamProcessorDLQ", ["Stream event-source-mapping OnFailure destination"], EVENT_FILL, EVENT_LINE)
component(cw, "CloudWatch Logs", ["2 Log Groups, RetentionInDays: 14", "structured JSON per invocation"], LOGS_FILL, LOGS_LINE)

# =========================================================================
# ARROWS
# =========================================================================
# generator -> S3 (data flow)
polyline_arrow([right_mid(gen), left_mid(s3)], DATA_COLOR)
label_on_line(((gen[2] + s3[0]) / 2, (gen[1] + gen[3]) / 2 - 22), "S3 PutObject", DATA_COLOR)

# S3 -> EventBridge (event trigger)
polyline_arrow([top_at(s3, 0.7), bottom_mid(eb)], TRIGGER_COLOR, dashed=True)
label_on_line(((s3[2] + eb[0]) / 2 + 20, (s3[1] + eb[3]) / 2 - 10), "Object Created", TRIGGER_COLOR)

# EventBridge -> decision-engine (event trigger / async invoke)
polyline_arrow([right_mid(eb), top_at(de, 0.25)], TRIGGER_COLOR, dashed=True)
label_on_line(((eb[2] + de[0]) / 2, eb[1] - 20), "invoke (async)", TRIGGER_COLOR)

# S3 -> decision-engine direct data read (distinct from the trigger path)
polyline_arrow([right_at(s3, 0.75), left_at(de, 0.75)], DATA_COLOR)
label_on_line(((s3[2] + de[0]) / 2, (s3[3] + de[3]) / 2 - 26), "GetObject (read NDJSON)", DATA_COLOR)

# decision-engine -> DecisionsTable (write)
polyline_arrow([right_at(de, 0.35), left_at(dt, 0.35)], WRITE_COLOR)
label_on_line(((de[2] + dt[0]) / 2, dt[1] + 25), "conditional PutItem", WRITE_COLOR)

# decision-engine -> its DLQ (failure)
polyline_arrow([bottom_at(de, 0.25), top_at(dedlq, 0.5)], FAIL_COLOR, dashed=True)
label_on_line((de[0] + 90, (de[3] + dedlq[1]) / 2), "on failure", FAIL_COLOR)

# decision-engine -> CustomerProfiles (READ)
polyline_arrow([bottom_at(de, 0.7), top_at(cp, 0.3)], READ_COLOR)
label_on_line(((de[0] + cp[0]) / 2 + 60, (de[3] + cp[1]) / 2), "READ", READ_COLOR)

# DecisionsTable -> downstream-processor (event trigger via stream) -- routed down the right side, clear of CustomerProfiles
stream_x = dt[0] + 60
polyline_arrow([(stream_x, dt[3]), (stream_x, dp[1] - 40), top_at(dp, 0.15)], TRIGGER_COLOR, dashed=True)
label_on_line((stream_x + 145, (dt[3] + dp[1]) / 2), "DynamoDB Stream (INSERT-only)", TRIGGER_COLOR)

# downstream-processor <-> CustomerProfiles (READ then WRITE)
polyline_arrow([top_at(dp, 0.35), right_at(cp, 0.75)], READ_COLOR)
polyline_arrow([right_at(cp, 0.9), top_at(dp, 0.5)], WRITE_COLOR)
label_on_line(((dp[0] + cp[2]) / 2 - 10, cp[3] + 26), "READ (then) WRITE", MUTED)

# downstream-processor -> DownstreamActionsTable (write)
polyline_arrow([right_at(dp, 0.4), left_at(da, 0.4)], WRITE_COLOR)
label_on_line(((dp[2] + da[0]) / 2, da[1] + 25), "conditional PutItem", WRITE_COLOR)

# downstream-processor -> its DLQ (failure)
polyline_arrow([bottom_at(dp, 0.3), top_at(dpdlq, 0.5)], FAIL_COLOR, dashed=True)
label_on_line((dp[0] + 90, (dp[3] + dpdlq[1]) / 2), "on failure", FAIL_COLOR)

# both lambdas -> CloudWatch Logs
polyline_arrow([bottom_at(de, 0.85), top_at(cw, 0.15)], LOGS_LINE, dashed=True, width=2)
polyline_arrow([bottom_at(dp, 0.85), top_at(cw, 0.85)], LOGS_LINE, dashed=True, width=2)
label_on_line((cw[0] - 55, ROW5_Y - 40), "logs", LOGS_LINE)
label_on_line((cw[2] + 55, ROW5_Y - 40), "logs", LOGS_LINE)

# =========================================================================
# CAPTION + LEGEND
# =========================================================================
caption_y = cw[3] + 70
text_center((W / 2, caption_y),
            "Order -> S3 -> Decision Engine -> Decision Store -> DynamoDB Stream -> Downstream Processor -> Downstream Actions",
            F_CAPTION, "#263238")

legend_top = caption_y + 40
d.rectangle([40, legend_top, W - 40, H - 30], outline="#BDBDBD", width=1)
text_center((150, legend_top + 26), "Legend", F_HEAD, INK)

items = [
    (STORE_FILL, STORE_LINE, "Persistent store (S3 / DynamoDB)"),
    (COMPUTE_FILL, COMPUTE_LINE, "Compute (Lambda)"),
    (EVENT_FILL, EVENT_LINE, "Eventing (EventBridge / SQS DLQ)"),
    (LOGS_FILL, LOGS_LINE, "Observability (CloudWatch Logs)"),
]
lx = 60
ly = legend_top + 55
for fill, line, label in items:
    box((lx, ly, lx + 26, ly + 26), fill, line, radius=4, width=2)
    d.text((lx + 36, ly + 4), label, font=F_LEGEND, fill=INK)
    tb = d.textbbox((0, 0), label, font=F_LEGEND)
    lx += 36 + (tb[2] - tb[0]) + 60

lines = [
    (DATA_COLOR, False, "S3 object read/write"),
    (TRIGGER_COLOR, True, "Event trigger"),
    (FAIL_COLOR, True, "Failure / DLQ path"),
    (READ_COLOR, False, "DynamoDB read"),
    (WRITE_COLOR, False, "DynamoDB write"),
]
lx = 60
ly = ly + 45
for color, dashed, label in lines:
    polyline_arrow([(lx, ly + 12), (lx + 50, ly + 12)], color, dashed=dashed, width=3)
    d.text((lx + 60, ly + 2), label, font=F_LEGEND, fill=INK)
    tb = d.textbbox((0, 0), label, font=F_LEGEND)
    lx += 60 + (tb[2] - tb[0]) + 60

os.makedirs("docs", exist_ok=True)
img.save("docs/architecture.png")
print("Wrote docs/architecture.png", img.size)
