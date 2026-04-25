import argparse
import html
import json
from pathlib import Path

def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def parse_collections(value):
    if not value:
        return []
    parts = []
    for item in value.split(","):
        item = item.strip()
        if item:
            parts.append(item)
    return parts

def human_bytes(size):
    if size is None:
        return "0 B"
    size = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

def build_report(files_tree, collections):
    manifest = files_tree.get("manifest", {})
    deps = manifest.get("deps", {})
    zip_files = files_tree.get("zip_files", {})

    requested = set(collections)

    missing_collections = [c for c in collections if c not in zip_files]

    common_archives = {}
    for archive, cols in deps.items():
        if requested.intersection(cols):
            common_archives[archive] = cols

    common_items = []
    total_common_size = 0
    for archive, cols in common_archives.items():
        archive_info = zip_files.get(archive, {})
        files_list = archive_info.get("files", [])
        archive_size = archive_info.get("size", 0)
        total_common_size += archive_size or 0

        files_sorted = sorted(
            files_list,
            key=lambda x: x.get("size", 0),
            reverse=True,
        )
        common_items.append({
            "archive": archive,
            "size": archive_size,
            "collections": sorted(set(cols)),
            "files": files_sorted,
        })

    common_items.sort(key=lambda x: x["size"], reverse=True)

    return {
        "requested": collections,
        "missing": missing_collections,
        "total_common_size": total_common_size,
        "common_items": common_items,
    }

def render_static_html(report):
    rows = []
    for item in report["common_items"]:
        file_rows = []
        for f in item["files"]:
            file_rows.append(
                f"<tr><td>{html.escape(f.get('path',''))}</td><td>{human_bytes(f.get('size',0))}</td><td>{html.escape(f.get('hexDigest',''))}</td></tr>"
            )
        file_table = "".join(file_rows) or "<tr><td colspan=\"3\">No files</td></tr>"

        rows.append(f"""
        <tr>
          <td>{html.escape(item['archive'])}</td>
          <td>{human_bytes(item['size'])}</td>
          <td>{html.escape(', '.join(item['collections']))}</td>
          <td>
            <table class=\"inner\">
              <thead><tr><th>File</th><th>Size</th><th>Hex</th></tr></thead>
              <tbody>{file_table}</tbody>
            </table>
          </td>
        </tr>
        """)

    missing_html = ""
    if report["missing"]:
        missing_html = "<p class=\"warn\">Missing collections: " + html.escape(", ".join(report["missing"])) + "</p>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Liveupdate Common Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #222; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ margin-bottom: 16px; }}
    .warn {{ color: #b00020; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
    th {{ background: #f6f6f6; text-align: left; }}
    .inner th, .inner td {{ font-size: 12px; }}
    .inner {{ width: 100%; }}
  </style>
</head>
<body>
  <h1>Liveupdate Common Report</h1>
  <div class=\"meta\">
    <div><strong>Collections:</strong> {html.escape(', '.join(report['requested']))}</div>
    <div><strong>Total common size:</strong> {human_bytes(report['total_common_size'])}</div>
    {missing_html}
  </div>
  <table>
    <thead>
      <tr>
        <th>Common Archive</th>
        <th>Size</th>
        <th>Used By Collections</th>
        <th>Files (sorted by size)</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>
"""

def render_interactive_html(files_tree):
    data_json = json.dumps(files_tree)
    html_template = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Liveupdate Common Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #222; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ margin-bottom: 16px; }}
    .warn {{ color: #b00020; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
    th {{ background: #f6f6f6; text-align: left; }}
    .inner th, .inner td {{ font-size: 12px; }}
    .inner {{ width: 100%; }}
    textarea {{ width: 100%; height: 80px; }}
    .controls {{ margin-bottom: 16px; }}
    .button {{ padding: 8px 12px; }}
  </style>
</head>
<body>
  <h1>Liveupdate Common Report</h1>
  <div class=\"controls\">
    <div>Collections (comma-separated):</div>
    <textarea id=\"collections\" placeholder=\"lobby_scene.collectionc,card_collection_window.collectionc\"></textarea>
    <button class=\"button\" id=\"run\">Build Report</button>
  </div>
  <div class=\"meta\" id=\"meta\"></div>
  <table id=\"report\" style=\"display:none;\">
    <thead>
      <tr>
        <th>Common Archive</th>
        <th>Size</th>
        <th>Used By Collections</th>
        <th>Files (sorted by size)</th>
      </tr>
    </thead>
    <tbody id=\"report-body\"></tbody>
  </table>

<script>
const FILES_TREE = __DATA_JSON__;

function humanBytes(size) {{
  if (size == null) return "0 B";
  let s = Number(size);
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (const u of units) {{
    if (s < 1024) return s.toFixed(2) + " " + u;
    s /= 1024;
  }}
  return s.toFixed(2) + " PB";
}}

function buildReport(collections) {{
  const manifest = FILES_TREE.manifest || {{}};
  const deps = manifest.deps || {{}};
  const zipFiles = FILES_TREE.zip_files || {{}};

  const requested = new Set(collections);
  const missing = collections.filter(c => !(c in zipFiles));

  const commonArchives = {{}};
  for (const [archive, cols] of Object.entries(deps)) {{
    if (cols.some(c => requested.has(c))) {{
      commonArchives[archive] = cols;
    }}
  }}

  let totalSize = 0;
  const items = [];
  for (const [archive, cols] of Object.entries(commonArchives)) {{
    const info = zipFiles[archive] || {{}};
    const files = (info.files || []).slice().sort((a,b) => (b.size||0) - (a.size||0));
    const size = info.size || 0;
    totalSize += size;
    items.push({{
      archive,
      size,
      collections: Array.from(new Set(cols)).sort(),
      files,
    }});
  }}

  items.sort((a,b) => b.size - a.size);

  return {{ requested: collections, missing, totalSize, items }};
}}

function render(report) {{
  const meta = document.getElementById('meta');
  const table = document.getElementById('report');
  const body = document.getElementById('report-body');

  const missingHtml = report.missing.length
    ? `<p class="warn">Missing collections: ${report.missing.join(', ')}</p>`
    : '';

  meta.innerHTML = `
    <div><strong>Collections:</strong> ${report.requested.join(', ')}</div>
    <div><strong>Total common size:</strong> ${humanBytes(report.totalSize)}</div>
    ${missingHtml}
  `;

  body.innerHTML = report.items.map(item => {
    const files = item.files.map(f =>
      `<tr><td>${f.path||''}</td><td>${humanBytes(f.size||0)}</td><td>${f.hexDigest||''}</td></tr>`
    ).join('') || '<tr><td colspan="3">No files</td></tr>';

    return `
      <tr>
        <td>${item.archive}</td>
        <td>${humanBytes(item.size)}</td>
        <td>${item.collections.join(', ')}</td>
        <td>
          <table class="inner">
            <thead><tr><th>File</th><th>Size</th><th>Hex</th></tr></thead>
            <tbody>${files}</tbody>
          </table>
        </td>
      </tr>
    `;
  }).join('');

  table.style.display = '';
}}

document.getElementById('run').addEventListener('click', () => {{
  const value = document.getElementById('collections').value;
  const collections = value.split(',').map(v => v.trim().replace(/^\"|\"$/g, '')).filter(Boolean);
  if (!collections.length) return;
  const report = buildReport(collections);
  render(report);
}});
</script>
</body>
</html>
"""
    html_template = html_template.replace("{{", "{").replace("}}", "}")
    return html_template.replace("__DATA_JSON__", data_json)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files-tree", default="dist/output/20.07/liveupdatelowres/files_tree.json")
    parser.add_argument("--collections", default="")
    parser.add_argument("--out", default="dist/output/20.07/liveupdatelowres/report.html")
    args = parser.parse_args()

    files_tree = load_json(Path(args.files_tree))
    collections = parse_collections(args.collections)

    if collections:
        report = build_report(files_tree, collections)
        html_text = render_static_html(report)
    else:
        html_text = render_interactive_html(files_tree)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    print(f"Report written to {out_path}")

if __name__ == "__main__":
    main()
