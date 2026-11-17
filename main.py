import base64
import csv
import hashlib
import html
import io
import re
import sqlite3

MAX_COLUMNS = 100
MAX_SQL_CHARS = 5000
TABLE = 'data'

_INT = re.compile(r'^[+-]?(0|[1-9]\d*)$')
_FLOAT = re.compile(r'^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$')


def load_csv(text, max_rows=10000):
    """Parse CSV text into {'columns': [...], 'types': [...], 'rows': [tuple, ...]}.

    Types are INTEGER, REAL or TEXT. Empty cells become None. Values with
    leading zeros (IDs, phone numbers) stay TEXT so they are not corrupted.
    """
    if not isinstance(text, str):
        raise ValueError('CSV input must be text')
    text = text.lstrip('\ufeff')
    try:
        records = [r for r in csv.reader(io.StringIO(text, newline='')) if any(c.strip() for c in r)]
    except csv.Error as error:
        raise ValueError('Invalid CSV: ' + str(error)) from error
    if not records:
        raise ValueError('CSV is empty')
    header, body = records[0], records[1:]
    if len(body) > max_rows:
        raise ValueError('CSV has more than %d data rows' % max_rows)

    width = len(header)
    keep = []
    for i in range(width):
        name = header[i].strip()
        has_data = any(i < len(r) and r[i].strip() for r in body)
        if name or has_data:
            keep.append(i)
    if not keep:
        raise ValueError('CSV has no columns')
    if len(keep) > MAX_COLUMNS:
        raise ValueError('CSV has more than %d columns' % MAX_COLUMNS)

    columns, seen = [], set()
    for n, i in enumerate(keep, 1):
        name = header[i].strip() or 'column_%d' % (i + 1)
        base, k = name, 2
        while name.lower() in seen:
            name = '%s_%d' % (base, k)
            k += 1
        seen.add(name.lower())
        columns.append(name)

    for r in body:
        if any(c.strip() for c in r[width:]):
            raise ValueError('Row has more cells than the header')

    raw = [[(r[i].strip() if i < len(r) else '') or None for i in keep] for r in body]
    types = []
    for c in range(len(columns)):
        vals = [row[c] for row in raw if row[c] is not None]
        if vals and all(_INT.match(v) for v in vals):
            types.append('INTEGER')
        elif vals and all(_FLOAT.match(v) for v in vals):
            types.append('REAL')
        else:
            types.append('TEXT')
    conv = {'INTEGER': int, 'REAL': float, 'TEXT': str}
    rows = [tuple(None if v is None else conv[types[c]](v) for c, v in enumerate(row)) for row in raw]
    return {'columns': columns, 'types': types, 'rows': rows}


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _words(s):
    return re.findall(r'[a-z0-9]+', s.lower())


def _mentions(question_words, phrase):
    pw = _words(phrase)
    if not pw:
        return False
    n = len(pw)
    for i in range(len(question_words) - n + 1):
        window = question_words[i:i + n]
        if window == pw or [w.rstrip('s') for w in window] == [w.rstrip('s') for w in pw]:
            return True
    return False


def plan_question(question, data):
    """Turn a plain-English question into one SQLite SELECT over table `data`.

    Deterministic and rule based: counts, sums, averages, min/max, top-N,
    group-by ("by"/"per"/"each"), simple value filters and numeric bounds.
    Raises ValueError when the question names nothing it can work with.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError('Question is empty')
    cols, types = data['columns'], data['types']
    q = question.lower()
    qw = _words(question)
    numeric = [c for c, t in zip(cols, types) if t in ('INTEGER', 'REAL')]
    text_cols = [c for c, t in zip(cols, types) if t == 'TEXT']

    group = None
    m = re.search(r'\b(?:by|per|each|for each|across)\b(.*)$', q)
    if m:
        tail = _words(m.group(1))
        for c in cols:
            if _mentions(tail, c):
                group = c
                break

    filters, filter_cols = {}, set()
    for c in text_cols:
        if c == group:
            continue
        distinct = {r[cols.index(c)] for r in data['rows'] if r[cols.index(c)] is not None}
        for v in sorted(distinct, key=len, reverse=True):
            if len(v) > 1 and _mentions(qw, v) and not any(v.lower() in s.lower() and v != s for s in filters.get(c, [])):
                filters.setdefault(c, []).append(v)
        if c in filters:
            filter_cols.add(c)

    stop = {'what', 'which', 'with', 'that', 'than', 'have', 'show', 'list', 'many', 'much', 'under', 'over',
            'below', 'above', 'less', 'more', 'most', 'least', 'cheapest', 'highest', 'lowest', 'average',
            'total', 'count', 'number', 'items', 'item', 'products', 'product', 'there', 'each', 'find',
            'give', 'from', 'top', 'first', 'bottom', 'expensive', 'largest', 'smallest', 'common'}
    colwords = {w.rstrip('s') for c in cols for w in _words(c)}
    used = {w for vals in filters.values() for v in vals for w in _words(v)}
    like = []
    for w in dict.fromkeys(qw):
        if w in used or w.rstrip('s') in {u.rstrip('s') for u in used}:
            continue
        if len(w) < 4 or w in stop or w.rstrip('s') in colwords or w.isdigit():
            continue
        for c in text_cols:
            if c == group or c in filters:
                continue
            i = cols.index(c)
            if any(r[i] is not None and w.rstrip('s') in _words(r[i].lower().replace('-', ' ')) + [x.rstrip('s') for x in _words(r[i])]
                   for r in data['rows']):
                like.append('%s LIKE %s' % (_q(c), _lit('%' + w.rstrip('s') + '%')))
                break

    metric = next((c for c in numeric if _mentions(qw, c) and c != group), None)
    bound_col = metric or (numeric[0] if numeric else None)

    where = []
    for c, vals in filters.items():
        where.append('%s IN (%s)' % (_q(c), ', '.join(_lit(v) for v in vals)) if len(vals) > 1
                     else '%s = %s' % (_q(c), _lit(vals[0])))
    if bound_col:
        for pat, op in ((r'(?:under|below|less than|cheaper than|fewer than|<)', '<'),
                        (r'(?:over|above|more than|greater than|at least|>)', '>')):
            mm = re.search(pat + r'\s*(-?\d[\d,]*\.?\d*)', q)
            if mm:
                where.append('%s %s %s' % (_q(bound_col), op, mm.group(1).replace(',', '')))
    where += like
    wsql = (' WHERE ' + ' AND '.join(where)) if where else ''

    top = re.search(r'\b(?:top|first|bottom)\s+(\d{1,4})\b', q)
    limit = int(top.group(1)) if top else None
    desc_words = r'\b(highest|most expensive|largest|biggest|maximum|max|greatest|top)\b'
    asc_words = r'\b(lowest|cheapest|smallest|minimum|min|least expensive|bottom)\b'
    is_desc, is_asc = re.search(desc_words, q), re.search(asc_words, q)
    mc = metric or bound_col

    def agg(fn):
        if not mc:
            raise ValueError('No numeric column to compute %s over' % fn)
        return '%s(%s)' % (fn, _q(mc)), '%s_%s' % (fn.lower(), re.sub(r'\W+', '_', mc.lower()).strip('_'))

    fn = None
    if re.search(r'\b(average|avg|mean)\b', q):
        fn = 'AVG'
    elif re.search(r'\b(sum|total)\b', q):
        fn = 'SUM'
    elif re.search(r'\b(how many|count|number of)\b', q):
        fn = 'COUNT'
    elif re.search(r'\b(max|maximum)\b', q) and not limit:
        fn = 'MAX'
    elif re.search(r'\b(min|minimum)\b', q) and not limit:
        fn = 'MIN'

    if group:
        if fn in ('AVG', 'SUM', 'MAX', 'MIN'):
            expr, alias = agg(fn)
            sel = '%s, %s AS %s' % (_q(group), expr, _q(alias))
            order = alias
        else:
            sel, order = '%s, COUNT(*) AS "count"' % _q(group), 'count'
        direction = 'ASC' if (is_asc and not is_desc) else 'DESC'
        lim = ' LIMIT %d' % limit if limit else ''
        return 'SELECT %s FROM %s%s GROUP BY %s ORDER BY %s %s%s' % (sel, TABLE, wsql, _q(group), _q(order), direction, lim)

    if fn == 'COUNT':
        return 'SELECT COUNT(*) AS "count" FROM %s%s' % (TABLE, wsql)
    if fn:
        expr, alias = agg(fn)
        return 'SELECT %s AS %s FROM %s%s' % (expr, _q(alias), TABLE, wsql)

    mm = re.search(r'\b(most|least) (?:common|popular|frequent)\b', q)
    target = next((c for c in text_cols if _mentions(qw, c)), None)
    if mm and target:
        d = 'DESC' if mm.group(1) == 'most' else 'ASC'
        return 'SELECT %s, COUNT(*) AS "count" FROM %s%s GROUP BY %s ORDER BY "count" %s LIMIT %d' % (
            _q(target), TABLE, wsql, _q(target), d, limit or 1)

    if (is_desc or is_asc) and mc:
        d = 'DESC' if is_desc and not is_asc else 'ASC'
        return 'SELECT * FROM %s%s ORDER BY %s %s LIMIT %d' % (TABLE, wsql, _q(mc), d, limit or 10)

    if where or re.search(r'\b(list|show|find|which|what|give|display|all)\b', q):
        if where or limit:
            return 'SELECT * FROM %s%s%s' % (TABLE, wsql, ' LIMIT %d' % limit if limit else '')
        return 'SELECT * FROM %s' % TABLE
    raise ValueError("Couldn't plan that question; try wording like 'average price by color' or pass --sql")


_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_BLOCKED_FUNCTIONS = {'load_extension', 'readfile', 'writefile', 'edit', 'fts3_tokenizer'}


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or arg1 or '').lower() in _BLOCKED_FUNCTIONS else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if (arg1 or '').lower() == TABLE and dbname in (None, 'main') else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _clean_sql(sql):
    if not isinstance(sql, str):
        raise ValueError('SQL must be text')
    sql = sql.strip()
    if sql.startswith('```'):
        sql = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', sql).strip()
    sql = sql.rstrip(';').strip()
    if not sql:
        raise ValueError('SQL is empty')
    if len(sql) > MAX_SQL_CHARS:
        raise ValueError('SQL is too long')
    if '\x00' in sql:
        raise ValueError('SQL contains a null byte')
    if not re.match(r'(?is)^(select|with)\b', sql):
        raise ValueError('Only a single read-only SELECT is allowed')
    return sql


def run_query(data, sql, max_rows=100, max_steps=100000):
    """Run one read-only SELECT over the data in an in-memory SQLite database.

    Returns a report dict (also written as the JSON receipt by the CLI):
      sql, columns, types, data (list of rows), rows (count returned),
      truncated (bool), source_rows, max_rows, max_steps.
    """
    if not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError('max_rows must be a positive integer')
    sql = _clean_sql(sql)
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE %s (%s)' % (TABLE, ', '.join('%s %s' % (_q(c), t) for c, t in zip(data['columns'], data['types']))))
        conn.executemany('INSERT INTO %s VALUES (%s)' % (TABLE, ','.join('?' * len(data['columns']))), data['rows'])
        conn.commit()
        try:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        except (AttributeError, sqlite3.Error):
            pass
        conn.set_authorizer(_authorizer)
        used = [0]
        tick = 100

        def budget():
            used[0] += tick
            return 1 if used[0] > max_steps else 0

        conn.set_progress_handler(budget, tick)
        try:
            cur = conn.execute(sql)
            fetched = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in (cur.description or [])]
        except sqlite3.OperationalError as error:
            if 'interrupt' in str(error).lower():
                raise ValueError('Query exceeded the %d step limit' % max_steps) from error
            raise ValueError('SQL error: ' + str(error)) from error
        except sqlite3.DatabaseError as error:
            msg = str(error)
            if 'not authorized' in msg.lower():
                raise ValueError('SQL not allowed (read-only SELECT over the data table only)') from error
            raise ValueError('SQL error: ' + msg) from error
        except (sqlite3.Warning, sqlite3.ProgrammingError) as error:
            raise ValueError('Only a single SELECT statement is allowed') from error
        if not columns:
            raise ValueError('Query returned no columns')
    finally:
        conn.close()

    truncated = len(fetched) > max_rows
    out = [[(v.hex() if isinstance(v, bytes) else v) for v in row] for row in fetched[:max_rows]]
    return {'sql': sql, 'columns': columns, 'types': list(data['types']), 'data': out, 'rows': len(out),
            'truncated': truncated, 'source_rows': len(data['rows']), 'max_rows': max_rows, 'max_steps': max_steps}


_PLAIN_NUMBER_COLUMNS = re.compile(r'(^|[\W_])(id|ids|year|zip|postcode|phone|code|sku|number)($|[\W_])', re.I)
_URL = re.compile(r'^https?://[^\s<>"\']+$', re.I)

_SCRIPT = r"""
(function () {
  var table = document.getElementById('t');
  if (!table) return;
  var tb = table.tBodies[0];
  var rows = Array.prototype.slice.call(tb.rows).filter(function (r) { return !r.classList.contains('empty'); });
  var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);
  var q = document.getElementById('q');
  var status = document.getElementById('status');
  var clear = document.getElementById('clear');
  rows.forEach(function (r, i) {
    r._i = i;
    r._t = Array.prototype.slice.call(r.cells, 1).map(function (c) { return c.textContent; }).join(' \u0001 ').toLowerCase();
  });
  var sortCol = -1, sortDir = 0;

  function key(row, c) {
    var cell = row.cells[c];
    if (cell.hasAttribute('data-null')) return null;
    if (cell.hasAttribute('data-n')) return parseFloat(cell.getAttribute('data-n'));
    return cell.textContent.toLowerCase();
  }
  function compare(a, b) {
    if (sortDir === 0) return a._i - b._i;
    var x = key(a, sortCol), y = key(b, sortCol);
    if (x === null && y === null) return a._i - b._i;
    if (x === null) return 1;
    if (y === null) return -1;
    var r = (typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
    return r === 0 ? a._i - b._i : r * sortDir;
  }
  function apply() {
    var needle = q.value.trim().toLowerCase();
    var words = needle ? needle.split(/\s+/) : [];
    var shown = 0;
    rows.slice().sort(compare).forEach(function (r) {
      var ok = words.every(function (w) { return r._t.indexOf(w) !== -1; });
      r.hidden = !ok;
      if (ok) { shown++; r.cells[0].textContent = shown; }
      tb.appendChild(r);
    });
    status.textContent = 'Showing ' + shown.toLocaleString() + ' of ' + rows.length.toLocaleString() + ' rows' +
      (sortDir ? ', sorted by ' + heads[sortCol].textContent + (sortDir > 0 ? ' (low to high)' : ' (high to low)') : '');
    clear.hidden = !needle;
    heads.forEach(function (h, i) {
      h.setAttribute('aria-sort', i === sortCol && sortDir ? (sortDir > 0 ? 'ascending' : 'descending') : 'none');
    });
  }
  heads.forEach(function (h, i) {
    if (i === 0) return;
    function go() {
      if (sortCol !== i) { sortCol = i; sortDir = 1; }
      else if (sortDir === 1) sortDir = -1;
      else if (sortDir === -1) { sortDir = 0; sortCol = -1; }
      apply();
    }
    h.addEventListener('click', go);
    h.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
  q.addEventListener('input', apply);
  clear.addEventListener('click', function () { q.value = ''; apply(); q.focus(); });
  var copy = document.getElementById('copy');
  if (copy) copy.addEventListener('click', function () {
    var text = document.getElementById('sql').textContent;
    function done() { copy.textContent = 'Copied'; setTimeout(function () { copy.textContent = 'Copy SQL'; }, 1500); }
    function fallback() {
      var r = document.createRange(); r.selectNodeContents(document.getElementById('sql'));
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      try { document.execCommand('copy'); done(); } catch (e) {}
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  });
  apply();
})();
"""

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#d8d8dc;--hd:#f3f3f5;--zebra:#fafafb;--hover:#eef3fb;--bar:#cfe0f7;--link:#0a58c2;--warn:#9a5b00;--acc:#0a58c2}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f0f0f2;--mut:#a0a0a8;--line:#3a3a40;--hd:#232327;--zebra:#1b1b1e;--hover:#22303f;--bar:#274468;--link:#7db3ff;--warn:#f0b050;--acc:#7db3ff}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:1300px;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
h1{font-size:22px;margin:0 0 2px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:20px 0 6px}
.meta{color:var(--mut);margin:0}
pre{background:var(--hd);border:1px solid var(--line);padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;margin:0 0 4px;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
.lim{color:var(--mut);font-size:12px;margin:0}
button{font:inherit;font-size:13px;text-transform:none;letter-spacing:0;color:var(--fg);background:var(--hd);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer;margin-left:6px}
button:hover{border-color:var(--acc)}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}
input[type=search]{font:inherit;flex:1 1 260px;max-width:420px;padding:6px 10px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px}
input[type=search]:focus,th:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
#status{color:var(--mut);font-size:13px}
.wrap{overflow:auto;max-height:75vh;border:1px solid var(--line);border-radius:6px}
table{border-collapse:separate;border-spacing:0;min-width:100%}
th,td{border-bottom:1px solid var(--line);padding:7px 12px;text-align:left;vertical-align:top}
th{background:var(--hd);position:sticky;top:0;z-index:2;white-space:nowrap;cursor:pointer;user-select:none}
th.idx{cursor:default}
th[aria-sort=ascending]::after{content:" \\25B2";font-size:10px;color:var(--acc)}
th[aria-sort=descending]::after{content:" \\25BC";font-size:10px;color:var(--acc)}
th:not(.idx):hover{background:var(--hover)}
tbody tr:nth-child(even){background:var(--zebra)}tbody tr:hover{background:var(--hover)}
tr[hidden]{display:none}
td.idx,th.idx{color:var(--mut);text-align:right;font-size:12px;min-width:3em}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;position:relative;min-width:6.5em}
td.num .bar{position:absolute;left:0;top:3px;bottom:3px;background:var(--bar);border-radius:0 3px 3px 0;z-index:0}
td.num .val{position:relative;z-index:1}
td.null{color:var(--mut);font-style:italic}
a{color:var(--link);word-break:break-all}
.warn{color:var(--warn);font-weight:600;margin:12px 0}
@media print{.wrap{max-height:none;overflow:visible}th{position:static}body{padding:0}.tools,button{display:none}}
"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{{DIGEST}}'">
<title>Data report</title>
<style>{{STYLE}}</style></head><body>
<h1>Data report</h1>
<p class="meta">{{META}}</p>
<h2>SQL receipt <button type="button" id="copy">Copy SQL</button></h2>
<pre id="sql">{{SQL}}</pre><p class="lim">{{LIMITS}}</p>
{{NOTE}}<h2>Results</h2>
<div class="tools"><input type="search" id="q" placeholder="Filter rows (all words must match)" aria-label="Filter rows" autocomplete="off">
<button type="button" id="clear" hidden>Clear</button><span id="status" aria-live="polite"></span></div>
<div class="wrap"><table id="t"><thead><tr>{{HEAD}}</tr></thead><tbody>
{{BODY}}
</tbody></table></div>
<script>{{SCRIPT}}</script>
</body></html>
"""


def render_report(report):
    """Render a run_query report as one self-contained, interactive HTML page.

    Click a column header to sort, type to filter rows, copy the SQL receipt.
    Every dynamic value is HTML-escaped (cells come from untrusted CSV data and
    the SQL may come from a model); the page's CSP only permits its own script
    by hash, and links are made only for http(s) URLs. For a two-column
    label/number result a CSS bar sits behind each number.
    """
    e = lambda v: html.escape('' if v is None else str(v), quote=True)
    columns, data = report['columns'], report['data']
    is_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)

    def fmt(col, v):
        if _PLAIN_NUMBER_COLUMNS.search(col):
            return str(v)
        if isinstance(v, int):
            return '{:,}'.format(v)
        if v != v or abs(v) == float('inf'):
            return str(v)
        return '{:,.2f}'.format(v).rstrip('0').rstrip('.')

    bars = len(columns) == 2 and data and all(is_num(r[1]) and r[1] == r[1] and r[1] >= 0 for r in data)
    top = max((r[1] for r in data), default=0) if bars else 0

    head = '<th scope="col" class="idx" title="Row number">#</th>' + ''.join(
        '<th scope="col" tabindex="0" role="button" aria-sort="none" title="Click to sort">%s</th>' % e(c) for c in columns)
    body = []
    for n, row in enumerate(data, 1):
        cells = ['<td class="idx">%d</td>' % n]
        for c, v in zip(columns, row):
            if v is None:
                cells.append('<td class="null" data-null="1">null</td>')
            elif is_num(v):
                finite = v == v and abs(v) != float('inf')
                bar = ''
                if bars and c == columns[1] and top > 0 and finite:
                    bar = '<span class="bar" style="width:%.1f%%"></span>' % (100.0 * v / top)
                attr = ' data-n="%s"' % e(repr(v)) if finite else ' data-null="1"'
                cells.append('<td class="num"%s title="%s">%s<span class="val">%s</span></td>' % (attr, e(v), bar, e(fmt(c, v))))
            elif isinstance(v, str) and _URL.match(v):
                cells.append('<td><a href="%s" target="_blank" rel="noopener noreferrer">%s</a></td>' % (e(v), e(v)))
            else:
                cells.append('<td>%s</td>' % e(v))
        body.append('<tr>%s</tr>' % ''.join(cells))
    if not body:
        body.append('<tr class="empty"><td colspan="%d" class="null">No rows matched.</td></tr>' % (len(columns) + 1))

    count, src = report['rows'], report.get('source_rows')
    meta = '%s row%s returned' % ('{:,}'.format(count), '' if count == 1 else 's')
    if src is not None:
        meta += ' from a table of %s' % '{:,}'.format(src)
    note = ''
    if report.get('truncated'):
        note = ('<p class="warn">Result truncated: showing the first %s rows, but more matched. '
                'Add a tighter filter or raise --limit.</p>' % e('{:,}'.format(count)))
    limits = 'row limit %s &middot; step limit %s' % (e(report.get('max_rows', '?')), e(report.get('max_steps', '?')))
    values = {
        'DIGEST': base64.b64encode(hashlib.sha256(_SCRIPT.encode('utf-8')).digest()).decode(),
        'STYLE': _STYLE, 'META': e(meta), 'SQL': e(report['sql']), 'LIMITS': limits, 'NOTE': note,
        'HEAD': head, 'BODY': '\n'.join(body), 'SCRIPT': _SCRIPT,
    }
    return re.sub(r'\{\{([A-Z]+)\}\}', lambda m: values[m.group(1)], _PAGE)