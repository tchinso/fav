"""Stage, verify, and publish a directory mirror without losing the last good backup."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from validate_backup import validate


SOURCE_URL = "https://fav.ju.mp/"
MANIFEST = "backup-manifest.json"
PROTECTED = {".git", ".github", "scripts", "tests", "README.md", "LICENSE",
             "search-list.json", MANIFEST}
KST = timezone(timedelta(hours=9))


def safe_path(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts
            or any("\\" in part or ":" in part for part in path.parts)
            or path.parts[0] in PROTECTED):
        raise ValueError(f"Unsafe mirror path: {name}")
    return Path(*path.parts)


def publish(mirror, repository):
    """Only replace validated, explicitly managed files; keep repository source files."""
    manifest_path = repository / MANIFEST
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "files": {"index.html": {}}
    }
    old_files = set(previous["files"])
    files = {}
    for file in sorted(mirror.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Unexpected symlink: {file}")
        if file.is_file():
            name = file.relative_to(mirror).as_posix()
            files[name] = {"bytes": file.stat().st_size,
                           "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    # Check every path before making any changes.
    paths = {name: safe_path(name) for name in old_files | files.keys()}
    for name, relative in paths.items():
        target = repository / relative
        if any(parent.is_symlink() for parent in [target, *target.parents]):
            raise ValueError(f"Refusing symlink destination: {name}")
        if name in files and target.exists() and name not in old_files:
            raise ValueError(f"Mirror would overwrite an unmanaged file: {name}")
    for name in files:
        target = repository / safe_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mirror / safe_path(name), target)
    for name in old_files - files.keys():
        target = repository / safe_path(name)
        if target.is_file():
            target.unlink()
    manifest = {
        "source_url": SOURCE_URL,
        "captured_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest


def download(mirror):
    # Crawl this site only. External bookmark destinations are not backup targets.
    command = [
        "wget", "--recursive", "--level=inf", "--no-parent", "--page-requisites",
        "--convert-links", "--adjust-extension", "--backup-converted",
        "--no-host-directories", "--force-directories", "--restrict-file-names=windows",
        "--execute=robots=off", "--reject-regex=/cdn-cgi/l/email-protection",
        "--domains=fav.ju.mp", "--https-only",
        "--dns-timeout=20", "--connect-timeout=20", "--read-timeout=60",
        "--tries=3", "--waitretry=15", "--retry-connrefused",
        "--retry-on-http-error=429,500,502,503,504", "--no-verbose",
        f"--directory-prefix={mirror}", SOURCE_URL,
    ]
    subprocess.run(command, check=True, timeout=480)
    original = mirror / "index.html.orig"
    # Wget creates .orig only when it converts links. For an already local page,
    # the downloaded index is also the original source.
    if not original.exists():
        original = mirror / "index.html"
    # Preview images in metadata are not Wget page requisites, but are part of
    # the site's files. Save same-site images without crawling bookmark sites.
    class PreviewImages(HTMLParser):
        urls = set()

        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag == "meta" and (attrs.get("property") or attrs.get("name")) in {
                "og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"
            }:
                url = urljoin(SOURCE_URL, attrs.get("content", ""))
                if urlsplit(url).netloc == "fav.ju.mp":
                    self.urls.add(url)

    previews = PreviewImages()
    previews.feed(original.read_text(encoding="utf-8"))
    if previews.urls:
        # Reuse request limits and filename rules, without recursive traversal.
        extra_command = [option for option in command[:-1]
                         if option not in {"--recursive", "--level=inf", "--page-requisites",
                                           "--convert-links", "--backup-converted"}]
        subprocess.run(extra_command + sorted(previews.urls), check=True, timeout=180)
    validate(original, mirror)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="Validated artifact directory")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=int, default=60)
    args = parser.parse_args()
    if args.attempts < 1 or args.retry_delay < 0:
        parser.error("attempts must be positive and retry-delay must be nonnegative")
    repository = args.repository.resolve()
    output = args.output.resolve()
    if output.exists():
        parser.error("output must be a new directory")
    for attempt in range(1, args.attempts + 1):
        try:
            with tempfile.TemporaryDirectory(prefix="fav-backup-") as temporary:
                mirror = Path(temporary) / "site"
                mirror.mkdir()
                download(mirror)
                # Validate the complete staged site before touching the repository.
                manifest = publish(mirror, repository)
                shutil.copytree(mirror, output)
                shutil.copyfile(repository / MANIFEST, output / MANIFEST)
            print(f"Saved {len(manifest['files'])} files at {manifest['captured_at_kst']}")
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as report:
                    report.write(f"Backed up {SOURCE_URL}\n\n"
                                 f"- Captured (KST): {manifest['captured_at_kst']}\n"
                                 f"- Verified files: {len(manifest['files'])}\n"
                                 "- Download the run's `fav-site` artifact for the full mirror.\n")
            return
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            print(f"Backup attempt {attempt}/{args.attempts} failed: {error}", flush=True)
            if attempt == args.attempts:
                raise SystemExit("Backup failed; no unverified download was published.") from error
            time.sleep(args.retry_delay)


if __name__ == "__main__":
    main()
