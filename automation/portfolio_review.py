"""Monthly GitHub portfolio review automation.

This script reviews public GitHub repositories for a professional portfolio.
It creates report files and suggested updates, then GitHub Actions opens a pull
request. It does not delete files and it does not directly push to the main
branch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_DIR = ROOT / "automation"
REPORTS_DIR = ROOT / "portfolio-reports"
SUGGESTIONS_DIR = ROOT / "suggested-updates"
BACKUP_DIR = AUTOMATION_DIR / "backups"
CODEX_SUMMARY_PATH = AUTOMATION_DIR / "codex_monthly_summary.md"


CAREER_KEYWORDS = [
    "aws",
    "cloud",
    "security",
    "cybersecurity",
    "linux",
    "python",
    "flask",
    "network",
    "networking",
    "bash",
    "support",
    "help desk",
    "iam",
    "vpc",
    "ec2",
    "s3",
    "cloudwatch",
    "log",
]


@dataclass
class RepoSummary:
    name: str
    full_name: str
    description: str
    html_url: str
    language: str
    updated_at: str
    pushed_at: str
    archived: bool
    fork: bool
    has_pages: bool
    topics: list[str]
    readme_status: str = "unknown"
    readme_notes: list[str] | None = None
    score: int = 0


@dataclass
class ActivitySummary:
    repo: str
    event_type: str
    message: str
    created_at: str


def github_api(path: str) -> Any:
    """Call the GitHub REST API with the workflow token when available."""
    token = os.getenv("GITHUB_TOKEN", "")
    request = Request(f"https://api.github.com{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"GitHub API request failed for {path}: {exc}") from exc


def backup_file(path: Path) -> None:
    """Create a timestamped backup before replacing a generated file."""
    if not path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_name = f"{path.stem}-{timestamp}{path.suffix}"
    shutil.copy2(path, BACKUP_DIR / backup_name)


def write_file(path: Path, content: str) -> None:
    """Write a generated file after backing up the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    # Keep the starter template from being reported as real monthly progress.
    if "Paste exported Codex chat summaries" in text and "Example:" in text:
        return ""
    return text


def ensure_codex_summary_file() -> None:
    """Create a monthly Codex summary template if the user has not added one."""
    if CODEX_SUMMARY_PATH.exists():
        return
    CODEX_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_SUMMARY_PATH.write_text(
        """# Codex Monthly Summary

Paste exported Codex chat summaries, project notes, completed tasks, generated
code notes, and lab work here before the monthly automation runs.

## New Projects

- 

## Completed Tasks

- 

## Skills Practiced

- 

## Screenshots or Evidence to Add

- 

## Notes for LinkedIn or Resume

- 
""",
        encoding="utf-8",
    )


def summarize_readme(repo: RepoSummary) -> tuple[str, list[str]]:
    """Check whether a README exists and identify common portfolio gaps."""
    notes: list[str] = []
    try:
        readme = github_api(f"/repos/{repo.full_name}/readme")
    except RuntimeError:
        return "missing", ["Add a README.md file."]

    size = int(readme.get("size", 0))
    if size < 500:
        notes.append("README looks short; add overview, setup, screenshots, and lessons learned.")

    # Download README text when content is available through the API.
    download_url = readme.get("download_url")
    text = ""
    if download_url:
        try:
            with urlopen(download_url, timeout=30) as response:
                text = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError):
            text = ""

    checks = {
        "Technologies Used": r"technolog|aws services used|tools used|topics",
        "Setup Steps": r"setup|install|run|step-by-step|process",
        "Screenshots": r"screenshot",
        "Lessons Learned": r"learned|what i learned",
        "Future Improvements": r"future improvement|next step",
        "Security Notes": r"security|best practices",
    }
    for label, pattern in checks.items():
        if text and not re.search(pattern, text, re.IGNORECASE):
            notes.append(f"Consider adding a {label} section.")

    return "present", notes


def score_repo(repo: RepoSummary) -> int:
    """Estimate portfolio value based on career alignment and completeness."""
    text = " ".join(
        [
            repo.name,
            repo.description,
            repo.language,
            " ".join(repo.topics),
        ]
    ).lower()

    score = 0
    score += sum(2 for keyword in CAREER_KEYWORDS if keyword in text)
    if repo.readme_status == "present":
        score += 4
    if repo.has_pages:
        score += 2
    if repo.archived:
        score -= 6
    if repo.fork:
        score -= 2
    if repo.readme_notes:
        score -= min(len(repo.readme_notes), 4)
    return score


def collect_repositories(username: str) -> list[RepoSummary]:
    raw_repos = github_api(f"/users/{username}/repos?per_page=100&type=owner&sort=updated")
    repos: list[RepoSummary] = []

    for item in raw_repos:
        repo = RepoSummary(
            name=item.get("name", ""),
            full_name=item.get("full_name", ""),
            description=item.get("description") or "",
            html_url=item.get("html_url", ""),
            language=item.get("language") or "",
            updated_at=item.get("updated_at", ""),
            pushed_at=item.get("pushed_at", ""),
            archived=bool(item.get("archived")),
            fork=bool(item.get("fork")),
            has_pages=bool(item.get("has_pages")),
            topics=item.get("topics") or [],
        )
        repo.readme_status, repo.readme_notes = summarize_readme(repo)
        repo.score = score_repo(repo)
        repos.append(repo)

    return repos


def collect_recent_activity(username: str) -> list[ActivitySummary]:
    """Collect recent public GitHub activity for the monthly progress summary."""
    raw_events = github_api(f"/users/{username}/events/public?per_page=30")
    activities: list[ActivitySummary] = []

    for item in raw_events:
        repo_name = item.get("repo", {}).get("name", "unknown repository")
        event_type = item.get("type", "Activity")
        created_at = item.get("created_at", "")
        payload = item.get("payload", {})
        message = event_type

        if event_type == "PushEvent":
            commits = payload.get("commits") or []
            commit_messages = [commit.get("message", "") for commit in commits[:2] if commit.get("message")]
            message = "; ".join(commit_messages) or "Pushed commits"
        elif event_type == "CreateEvent":
            ref_type = payload.get("ref_type", "item")
            ref = payload.get("ref")
            message = f"Created {ref_type}" + (f" {ref}" if ref else "")
        elif event_type == "PullRequestEvent":
            action = payload.get("action", "updated")
            title = payload.get("pull_request", {}).get("title", "pull request")
            message = f"{action.title()} PR: {title}"

        activities.append(
            ActivitySummary(
                repo=repo_name,
                event_type=event_type,
                message=message,
                created_at=created_at,
            )
        )

    return activities


def select_featured_repos(repos: list[RepoSummary]) -> list[RepoSummary]:
    """Return the strongest repos to consider for profile pins and README features."""
    candidates = [repo for repo in repos if not repo.archived and not repo.fork]
    return sorted(candidates, key=lambda repo: repo.score, reverse=True)[:6]


def format_repo_table(repos: list[RepoSummary]) -> str:
    if not repos:
        return "No repositories found for this section."

    rows = [
        "| Repository | Language | Status | Portfolio Notes |",
        "| --- | --- | --- | --- |",
    ]
    for repo in repos:
        status = "Archived" if repo.archived else "Active"
        notes = "; ".join(repo.readme_notes or []) or "Looks usable for portfolio review."
        rows.append(f"| [{repo.name}]({repo.html_url}) | {repo.language or 'N/A'} | {status} | {notes} |")
    return "\n".join(rows)


def format_activity_table(activities: list[ActivitySummary]) -> str:
    rows = [
        "| Date | Repository | Activity | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for activity in activities[:10]:
        date = activity.created_at[:10] if activity.created_at else "N/A"
        rows.append(f"| {date} | {activity.repo} | {activity.event_type} | {activity.message} |")
    return "\n".join(rows) if len(rows) > 2 else "No recent public GitHub activity found."


def stale_repositories(repos: list[RepoSummary]) -> list[RepoSummary]:
    """Find active repositories that may need review because they have not changed recently."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    stale: list[RepoSummary] = []
    for repo in repos:
        if repo.archived:
            continue
        try:
            pushed_at = datetime.fromisoformat(repo.pushed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if pushed_at < cutoff:
            stale.append(repo)
    return stale[:10]


def build_monthly_report(repos: list[RepoSummary], activities: list[ActivitySummary], codex_notes: str) -> str:
    now = datetime.now(timezone.utc).strftime("%B %Y")
    featured = select_featured_repos(repos)

    report = [
        f"# Monthly GitHub Portfolio Review - {now}",
        "",
        "## Purpose",
        "",
        "Review GitHub portfolio progress and suggest professional updates for IT support, cybersecurity, cloud computing, Linux, Python, and networking roles.",
        "",
        "## Strong Repositories to Feature",
        "",
        format_repo_table(featured),
        "",
        "## Recent GitHub Activity",
        "",
        format_activity_table(activities),
        "",
        "## Active Repositories Needing Attention",
        "",
        format_repo_table([repo for repo in repos if repo.readme_notes and not repo.archived][:10]),
        "",
        "## Outdated Information to Review",
        "",
        format_repo_table(stale_repositories(repos)),
        "",
        "## Archived or Low-Priority Repositories",
        "",
        format_repo_table([repo for repo in repos if repo.archived][:10]),
        "",
        "## Suggested Monthly Actions",
        "",
        "- Review new repositories and decide if they should be public, private, archived, or improved.",
        "- Add screenshots only after removing sensitive information.",
        "- Keep project READMEs clear: overview, technologies, setup, screenshots, lessons learned, and future improvements.",
        "- Update the profile README when a new strong project, certificate, or technical skill is ready.",
        "- Update blogIT138 when a project becomes portfolio-ready.",
        "",
        "## Codex Monthly Notes",
        "",
        codex_notes or "No Codex monthly summary was provided this month.",
    ]
    return "\n".join(report)


def build_profile_suggestions(repos: list[RepoSummary], codex_notes: str) -> str:
    featured = select_featured_repos(repos)
    lines = [
        "# Suggested GitHub Profile README Updates",
        "",
        "Use these suggestions after manual review. Do not paste sensitive information.",
        "",
        "## Suggested Featured Projects",
        "",
    ]
    for repo in featured:
        lines.extend(
            [
                f"### {repo.name}",
                repo.description or "Add a short professional project description.",
                "",
                f"- Repository: {repo.html_url}",
                f"- Skills: {', '.join(repo.topics) if repo.topics else repo.language or 'Add skills used'}",
                "",
            ]
        )

    lines.extend(
        [
            "## Suggested Skills to Keep Current",
            "",
            "- AWS, IAM, VPC, EC2, S3, CloudWatch",
            "- Linux, Bash, SSH, logs, permissions",
            "- Python, Flask, Git, GitHub",
            "- Cybersecurity fundamentals, access control, log review",
            "- Networking, DNS, IP addressing, troubleshooting",
            "- IT support, documentation, Microsoft Office",
            "",
            "## Codex Notes to Consider",
            "",
            codex_notes or "No Codex notes provided.",
        ]
    )
    return "\n".join(lines)


def build_blog_suggestions(repos: list[RepoSummary], codex_notes: str) -> str:
    featured = select_featured_repos(repos)
    lines = [
        "# Suggested blogIT138 Website Updates",
        "",
        "Review these before editing the GitHub Pages site.",
        "",
        "## Project Cards to Feature",
        "",
    ]
    for repo in featured:
        lines.append(f"- **{repo.name}:** {repo.description or 'Add a clear project summary.'} Link: {repo.html_url}")

    lines.extend(
        [
            "",
            "## Blog Post Ideas",
            "",
            "- What I learned from AWS IAM, VPC, EC2, S3, and CloudWatch labs",
            "- How I used Linux logs for basic SSH security review",
            "- How I improved my GitHub portfolio for IT support and cybersecurity roles",
            "- How I organize README files for professional portfolio projects",
            "",
            "## Codex Work to Consider",
            "",
            codex_notes or "No Codex monthly summary was provided.",
        ]
    )
    return "\n".join(lines)


def build_linkedin_descriptions(repos: list[RepoSummary]) -> str:
    featured = select_featured_repos(repos)
    lines = [
        "# LinkedIn-Friendly Project Descriptions",
        "",
        "Use these as starting points for LinkedIn posts or Featured section descriptions.",
        "",
    ]
    for repo in featured:
        skills = ", ".join(repo.topics) if repo.topics else repo.language or "technical documentation"
        lines.extend(
            [
                f"## {repo.name}",
                "",
                f"I worked on **{repo.name}** to practice {skills}. "
                f"This project helped me improve my technical documentation, problem solving, and portfolio presentation for IT, cybersecurity, and cloud computing roles.",
                "",
                f"Repository: {repo.html_url}",
                "",
            ]
        )
    return "\n".join(lines)


def build_manual_checklist() -> str:
    return """# Monthly Manual Portfolio Checklist

Complete this before running the monthly automation or before reviewing its pull request.

## LinkedIn Updates

- [ ] New job, volunteer, class, or project experience
- [ ] New certificates or learning paths
- [ ] New skills to highlight
- [ ] New LinkedIn posts or project descriptions

## GitHub Updates

- [ ] New repositories created this month
- [ ] Repositories that should be pinned
- [ ] Repositories that should be archived or made private
- [ ] Project screenshots to add
- [ ] README files that need stronger documentation

## Codex Updates

- [ ] Paste exported Codex chat summaries into `automation/codex_monthly_summary.md`
- [ ] List generated code or scripts that became useful projects
- [ ] List completed labs or troubleshooting tasks
- [ ] Note any weak, unfinished, or duplicate work that should stay off the public portfolio

## Security Review

- [ ] Remove AWS account IDs from screenshots
- [ ] Remove public IP addresses and instance IDs
- [ ] Remove access keys, secret values, `.pem` files, and credentials
- [ ] Check that screenshots do not expose personal or school account information
"""


def main() -> None:
    username = os.getenv("GITHUB_USERNAME", "khalidshams-tech")
    ensure_codex_summary_file()
    codex_notes = read_optional(CODEX_SUMMARY_PATH)
    repos = collect_repositories(username)
    activities = collect_recent_activity(username)

    write_file(REPORTS_DIR / "monthly-portfolio-review.md", build_monthly_report(repos, activities, codex_notes))
    write_file(SUGGESTIONS_DIR / "profile-readme-suggestions.md", build_profile_suggestions(repos, codex_notes))
    write_file(SUGGESTIONS_DIR / "blogIT138-content-suggestions.md", build_blog_suggestions(repos, codex_notes))
    write_file(SUGGESTIONS_DIR / "linkedin-project-descriptions.md", build_linkedin_descriptions(repos))
    write_file(AUTOMATION_DIR / "monthly_manual_checklist.md", build_manual_checklist())


if __name__ == "__main__":
    main()
