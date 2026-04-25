import os
import json


def format_size_kb(size_bytes):
    return round(size_bytes / 1024, 2)


def generate_all_archives_html_report(report_data, output_path, current_timestamp, max_archive_size):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
        f.write("<meta charset=\"utf-8\" />\n")
        f.write("<title>All Archives Report with Manifests</title>\n")
        f.write(_get_html_styles())
        f.write("</head>\n<body>\n")
        
        f.write(f"<h1>All Archives Report with Manifests</h1>\n")
        f.write(f"<p>Generated at: <strong>{current_timestamp}</strong></p>\n")
        f.write(f"<p>Max archive size limit: <strong>{max_archive_size} bytes (~{format_size_kb(max_archive_size)} KB)</strong></p>\n")
        f.write(f"<p><strong>Total archives:</strong> {report_data['total_archives']} ")
        f.write(f"(<span class='badge common'>{report_data['common_archives']} common</span> ")
        f.write(f"<span class='badge collection'>{report_data['collection_archives']} collections</span>)</p>\n")

        sorted_archives = sorted(report_data["archives"], key=lambda x: x["zip_size_bytes"], reverse=True)
        
        for archive in sorted_archives:
            _write_archive_section(f, archive, include_type_badge=True)

        f.write("</body></html>")


def generate_common_archives_html_report(report_data, output_path, current_timestamp, max_archive_size):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
        f.write("<meta charset=\"utf-8\" />\n")
        f.write("<title>Common archives report</title>\n")
        f.write(_get_html_styles())
        f.write("</head>\n<body>\n")
        
        f.write(f"<h1>Common archives report</h1>\n")
        f.write(f"<p>Generated at: <strong>{current_timestamp}</strong></p>\n")
        f.write(f"<p>Max archive size limit for common files: <strong>{max_archive_size} bytes (~{format_size_kb(max_archive_size)} KB)</strong></p>\n")
        f.write("<p><em>Archives are sorted by size (largest first). Resources within each archive are also sorted by size (largest first).</em></p>\n")
        f.write(f"<p><strong>Total archives:</strong> {len(report_data['archives'])}</p>\n")

        sorted_archives = sorted(report_data["archives"], key=lambda x: x["zip_size_bytes"], reverse=True)
        
        for archive in sorted_archives:
            _write_archive_section(f, archive, include_type_badge=False)

        f.write("</body></html>")


def _get_html_styles():
    return """
<style>
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 20px;
    background: #111;
    color: #eee;
}
h1, h2 {
    color: #fff;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin-bottom: 32px;
}
th, td {
    border: 1px solid #444;
    padding: 6px 8px;
    font-size: 13px;
}
th {
    background: #222;
}
tr:nth-child(even) {
    background: #181818;
}
code {
    font-family: Consolas, "Courier New", monospace;
    font-size: 12px;
}
.badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 4px;
    background: #333;
    margin: 2px;
    font-size: 11px;
}
.badge.common {
    background: #4CAF50;
    color: white;
}
.badge.collection {
    background: #2196F3;
    color: white;
}
.archive-header {
    background: #1a1a1a;
    padding: 10px;
    border-radius: 4px;
    margin: 10px 0;
}
.size-large {
    font-weight: bold;
    color: #ff6b6b;
}
.size-medium {
    color: #feca57;
}
.size-small {
    color: #48dbfb;
}
.manifest-details {
    background: #0a0a0a;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 8px;
    margin: 8px 0;
    font-size: 12px;
}
.manifest-resources {
    max-height: 200px;
    overflow-y: auto;
    background: #222;
    border: 1px solid #444;
    padding: 8px;
    margin: 4px 0;
}
</style>
"""


def _write_archive_section(f, archive, include_type_badge=False):
    size_kb = archive['zip_size_kb']
    size_class = 'size-large' if size_kb > 5120 else 'size-medium' if size_kb > 1024 else 'size-small'
    
    f.write(f"<div class='archive-header'>\n")
    f.write(f"<h2>{archive['archive_name']}")
    
    if include_type_badge:
        archive_type = 'common' if archive.get('is_common_archive', False) else 'collection'
        f.write(f" <span class='badge {archive_type}'>{archive_type}</span>")
    
    f.write("</h2>\n")
    f.write("<ul>")
    f.write(f"<li>Zip file: <code>{archive['zip_file']}</code></li>")
    f.write(f"<li>Total size: <span class='{size_class}'>{archive['zip_size_kb']} KB</span> ({archive['zip_size_bytes']} bytes)</li>")
    f.write(f"<li>DManifest size: <span class='size-small'>{archive.get('dmanifest_size_kb', 0)} KB</span> ({archive.get('dmanifest_size_bytes', 0)} bytes)</li>")
    f.write(f"<li>Resources size: <span class='{size_class}'>{archive.get('resources_total_size_kb', 0)} KB</span> ({archive.get('resources_total_size_bytes', 0)} bytes)</li>")
    
    if archive.get("dependent_collections"):
        f.write("<li>Dependent collections: ")
        f.write(", ".join(f"<span class='badge'>{c}</span>" for c in archive["dependent_collections"]))
        f.write("</li>")
    else:
        f.write("<li>Dependent collections: <em>none</em></li>")
                
    f.write("</ul>")
    f.write("</div>")

    f.write("""
<table>
  <thead>
    <tr>
      <th>#</th>
      <th>Resource path</th>
      <th>Size (KB)</th>
      <th>Used in collections</th>
    </tr>
  </thead>
  <tbody>
""")
    for idx, res in enumerate(archive["resources"], 1):
        used = ", ".join(f"<span class='badge'>{c}</span>" for c in res["used_in_collections"]) or "<em>-</em>"
        
        res_size_kb = res['resource_size_kb'] 
        size_class = 'size-large' if res_size_kb > 1024 else 'size-medium' if res_size_kb > 100 else 'size-small'
        
        f.write("<tr>")
        f.write(f"<td>{idx}</td>")
        f.write(f"<td><code>{res['resource_path']}</code></td>")
        f.write(f"<td><span class='{size_class}'>{res['resource_size_kb']}</span></td>")
        f.write(f"<td>{used}</td>")
        f.write("</tr>\n")
    f.write("</tbody></table>\n")


def prepare_archives_report_data(all_archives_report, dependency_list, current_timestamp, max_archive_size, include_stats=True):
    archive_dependencies = {}
    for main_file, archives in dependency_list.items():
        for arch in archives:
            if arch not in archive_dependencies:
                archive_dependencies[arch] = set()
            archive_dependencies[arch].add(main_file)

    for arch_name, arch_info in all_archives_report.items():
        deps = archive_dependencies.get(arch_name, set())
        arch_info["dependent_collections"] = sorted(os.path.basename(p) for p in deps)

    report_data = {
        "generated_at": current_timestamp,
        "max_archive_size_bytes": max_archive_size,
        "archives": []
    }
    
    if include_stats:
        report_data["total_archives"] = len(all_archives_report)
        report_data["common_archives"] = len([a for a in all_archives_report.values() if a.get("is_common_archive", False)])
        report_data["collection_archives"] = len([a for a in all_archives_report.values() if not a.get("is_common_archive", False)])

    for arch_name in sorted(all_archives_report.keys()):
        arch_info = all_archives_report[arch_name]
        archive_record = {
            "archive_name": arch_info["archive_name"],
            "zip_file": arch_info["zip_file"],
            "zip_size_bytes": arch_info["zip_size_bytes"],
            "zip_size_kb": format_size_kb(arch_info["zip_size_bytes"]),
            "dmanifest_size_bytes": arch_info.get("dmanifest_size_bytes", 0),
            "dmanifest_size_kb": format_size_kb(arch_info.get("dmanifest_size_bytes", 0)),
            "resources_total_size_bytes": arch_info.get("resources_total_size_bytes", 0),
            "resources_total_size_kb": format_size_kb(arch_info.get("resources_total_size_bytes", 0)),
            "dependent_collections": arch_info.get("dependent_collections", []),
            "manifest_info": arch_info.get("manifest_info", {}),
            "resources": []
        }
        
        if include_stats:
            archive_record["is_common_archive"] = arch_info.get("is_common_archive", False)

        sorted_resources = sorted(arch_info["resources"], key=lambda x: x["resource_size_bytes"], reverse=True)
        
        for res in sorted_resources:
            archive_record["resources"].append({
                "resource_path": res["resource_path"],
                "resource_size_bytes": res["resource_size_bytes"],
                "resource_size_kb": format_size_kb(res["resource_size_bytes"]),
                "used_in_collections": [os.path.basename(p) for p in res["used_in_collections"]],
            })

        report_data["archives"].append(archive_record)

    return report_data


def generate_all_archives_report(all_archives_report, dependency_list, result_folder, current_timestamp, max_archive_size):
    if not all_archives_report:
        print("No archives to report.")
        return
    
    report_data = prepare_archives_report_data(
        all_archives_report, 
        dependency_list, 
        current_timestamp, 
        max_archive_size,
        include_stats=True
    )

    report_json_path = os.path.join(result_folder, "all_archives_report.json")
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4)
    print(f"All archives JSON report written to: {report_json_path}")

    report_html_path = os.path.join(result_folder, "all_archives_report.html")
    generate_all_archives_html_report(report_data, report_html_path, current_timestamp, max_archive_size)
    print(f"All archives HTML report written to: {report_html_path}")


def generate_common_report_files(common_archives_report, dependency_list, result_folder, current_timestamp, max_archive_size):
    if not common_archives_report:
        print("No common archives to report.")
        return
    
    report_data = prepare_archives_report_data(
        common_archives_report,
        dependency_list,
        current_timestamp,
        max_archive_size,
        include_stats=False
    )

    report_json_path = os.path.join(result_folder, "common_report.json")
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4)
    print(f"Common JSON report written to: {report_json_path}")

    report_html_path = os.path.join(result_folder, "common_report.html")
    generate_common_archives_html_report(report_data, report_html_path, current_timestamp, max_archive_size)
    print(f"Common HTML report written to: {report_html_path}")
