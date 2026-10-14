import os
import shutil
import zipfile

src_dir = os.path.dirname(os.path.abspath(__file__))
desktop_dir = os.path.expanduser(r"~\Desktop")
desktop_zip = os.path.join(desktop_dir, "SpeedBond_Final_Release.zip")
desktop_report = os.path.join(desktop_dir, "SpeedBond_Final_Engineering_Report.md")
desktop_spec = os.path.join(desktop_dir, "speedbooster.md")

# Excluded directories and file patterns
exclude_dirs = {".pytest_cache", "__pycache__", "bench_output", "benchmarks", ".git", ".vscode"}
exclude_extensions = {".pyc", ".speedbond_part", ".bin", ".zip", ".tmp"}

# Clean any existing desktop zip
if os.path.exists(desktop_zip):
    os.remove(desktop_zip)

print(f"Building clean final archive: {desktop_zip}")
added_files = []

with zipfile.ZipFile(desktop_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src_dir):
        # Filter directories in place
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in exclude_extensions or f.startswith("."):
                continue
            abs_p = os.path.join(root, f)
            rel_p = os.path.relpath(abs_p, src_dir)
            # Ensure no nested zip inside deliverables
            if rel_p.endswith(".zip"):
                continue
            zf.write(abs_p, rel_p)
            added_files.append(rel_p)

zip_size = os.path.getsize(desktop_zip)
print(f"Successfully packaged {len(added_files)} files into {desktop_zip} ({zip_size:,} bytes)")

# Synchronize reports to Desktop
report_src = os.path.join(src_dir, "deliverables", "SpeedBond_Final_Engineering_Report.md")
spec_src = os.path.join(src_dir, "speedbooster.md")

shutil.copy2(report_src, desktop_report)
shutil.copy2(spec_src, desktop_spec)
print(f"Synchronized final engineering report to: {desktop_report}")
print(f"Synchronized technical specification to: {desktop_spec}")
