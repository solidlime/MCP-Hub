#!/usr/bin/env python3
"""
Standalone benchmark comparing tool-search approaches (BM25, fastembed, TF-IDF, hybrids, etc.).

Self-contained. Run:  python .benchmarks/search_approaches_benchmark.py

Dependencies: rank_bm25, numpy, scikit-learn, fastembed (optional), py_rust_stemmers (fastembed dep)
"""

from __future__ import annotations

import logging
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("benchmark")

# ═══════════════════════════════════════════════════════════════════
# 1.  TEST DATA
# ═══════════════════════════════════════════════════════════════════

TOOLS: list[dict[str, Any]] = [
    # ── filesystem ──────────────────────────────────────────────
    {
        "server": "filesystem",
        "name": "read_file",
        "description": "Read file contents from disk. Returns the full text content of a file at the given path.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file to read"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "write_file",
        "description": "Write content to a file on disk. Creates or overwrites a file at the specified path.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file to write"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "list_directory",
        "description": "List files and subdirectories in a directory. Returns file names, sizes, and modification times.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "create_directory",
        "description": "Create a new directory at the specified path. Also creates parent directories if needed.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "delete_file",
        "description": "Delete a file from disk permanently. Use with caution as this cannot be undone.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Path to the file to delete"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "move_file",
        "description": "Move or rename a file or directory from source to destination path.",
        "inputSchema": {
            "properties": {
                "source": {"type": "string", "description": "Current path of the file"},
                "destination": {"type": "string", "description": "Target path to move the file to"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "get_file_info",
        "description": "Get file metadata and stats including size, permissions, creation time, and modification time.",
        "inputSchema": {
            "properties": {
                "path": {"type": "string", "description": "Path to the file or directory"},
            },
        },
    },
    {
        "server": "filesystem",
        "name": "search_files",
        "description": "Search for files matching a glob pattern. Recursively searches subdirectories.",
        "inputSchema": {
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match (e.g., '**/*.py')"},
                "base_path": {"type": "string", "description": "Root directory to start search from"},
            },
        },
    },
    # ── github ──────────────────────────────────────────────────
    {
        "server": "github",
        "name": "create_issue",
        "description": "Create a new GitHub issue in a repository with title, body, labels, and assignees.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue description body"},
            },
        },
    },
    {
        "server": "github",
        "name": "search_repos",
        "description": "Search GitHub repositories by query string. Returns matching repos with metadata.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g., 'machine learning language:python')"},
            },
        },
    },
    {
        "server": "github",
        "name": "get_file_contents",
        "description": "Get file contents from a GitHub repository at a specific branch or commit.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "path": {"type": "string", "description": "File path within the repo"},
                "ref": {"type": "string", "description": "Branch, tag, or commit SHA"},
            },
        },
    },
    {
        "server": "github",
        "name": "create_pr",
        "description": "Create a pull request on a GitHub repository with title, body, and branch details.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "title": {"type": "string", "description": "PR title"},
                "head": {"type": "string", "description": "Source branch name"},
                "base": {"type": "string", "description": "Target branch name"},
            },
        },
    },
    {
        "server": "github",
        "name": "list_issues",
        "description": "List issues in a GitHub repository with optional filtering by state, label, or assignee.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Issue state filter"},
            },
        },
    },
    {
        "server": "github",
        "name": "get_issue",
        "description": "Get details of a specific GitHub issue by issue number.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "issue_number": {"type": "integer", "description": "Issue number"},
            },
        },
    },
    {
        "server": "github",
        "name": "add_comment",
        "description": "Add a comment to an existing issue or pull request on GitHub.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "issue_number": {"type": "integer", "description": "Issue or PR number"},
                "body": {"type": "string", "description": "Comment text"},
            },
        },
    },
    {
        "server": "github",
        "name": "list_prs",
        "description": "List pull requests in a GitHub repository with state filtering.",
        "inputSchema": {
            "properties": {
                "repo": {"type": "string", "description": "Repository name (owner/repo format)"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "PR state filter"},
            },
        },
    },
    # ── brave-search ────────────────────────────────────────────
    {
        "server": "brave-search",
        "name": "brave_web_search",
        "description": "Search the web using the Brave Search API. Returns web results with titles, snippets, and URLs.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Web search query"},
                "count": {"type": "integer", "description": "Number of results to return (max 20)"},
            },
        },
    },
    {
        "server": "brave-search",
        "name": "brave_local_search",
        "description": "Search for local businesses and places using Brave Search's local results.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Local search query (e.g., 'coffee shops in Tokyo')"},
            },
        },
    },
    # ── puppeteer ───────────────────────────────────────────────
    {
        "server": "puppeteer",
        "name": "puppeteer_navigate",
        "description": "Navigate to a URL in the browser headless instance. Waits for the page to fully load.",
        "inputSchema": {
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
        },
    },
    {
        "server": "puppeteer",
        "name": "puppeteer_screenshot",
        "description": "Take a screenshot of the current web page. Returns the image as a base64-encoded string.",
        "inputSchema": {
            "properties": {
                "full_page": {"type": "boolean", "description": "Capture full page or viewport only"},
            },
        },
    },
    {
        "server": "puppeteer",
        "name": "puppeteer_click",
        "description": "Click on a page element identified by a CSS selector.",
        "inputSchema": {
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element to click"},
            },
        },
    },
    {
        "server": "puppeteer",
        "name": "puppeteer_fill",
        "description": "Fill in a form input field with text content.",
        "inputSchema": {
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input field"},
                "value": {"type": "string", "description": "Text to fill in"},
            },
        },
    },
    {
        "server": "puppeteer",
        "name": "puppeteer_evaluate",
        "description": "Execute JavaScript code in the context of the current page and return the result.",
        "inputSchema": {
            "properties": {
                "script": {"type": "string", "description": "JavaScript code to execute"},
            },
        },
    },
    # ── nous ────────────────────────────────────────────────────
    {
        "server": "nous",
        "name": "get_context",
        "description": "Retrieve the current context state and memory. Returns all stored context data for the active session.",
        "inputSchema": {
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific keys to retrieve; returns all if empty",
                },
            },
        },
    },
    {
        "server": "nous",
        "name": "update_context",
        "description": "Update context with new information. Merges new data into the existing context state.",
        "inputSchema": {
            "properties": {
                "data": {"type": "object", "description": "Key-value pairs to merge into context"},
            },
        },
    },
    {
        "server": "nous",
        "name": "memory_create",
        "description": "Create a new memory entry for later recall. Stores a named fact or observation in long-term memory.",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Unique identifier for the memory"},
                "value": {"type": "string", "description": "Content to store"},
            },
        },
    },
    {
        "server": "nous",
        "name": "memory_search",
        "description": "Search through stored memories by semantic similarity. Returns the most relevant memory entries.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Natural language query to search memories"},
                "limit": {"type": "integer", "description": "Maximum number of results"},
            },
        },
    },
    {
        "server": "nous",
        "name": "memory_delete",
        "description": "Remove a memory entry by its key. Permanently deletes the stored memory.",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Key of the memory to delete"},
            },
        },
    },
    {
        "server": "nous",
        "name": "context_clear",
        "description": "Clear all context data. Resets the session context to an empty state.",
    },
    {
        "server": "nous",
        "name": "context_append",
        "description": "Append data to the current context without overwriting existing values.",
        "inputSchema": {
            "properties": {
                "data": {"type": "object", "description": "Data to append to the context"},
            },
        },
    },
    # ── slack ───────────────────────────────────────────────────
    {
        "server": "slack",
        "name": "send_message",
        "description": "Send a message to a Slack channel. Supports markdown formatting and rich text.",
        "inputSchema": {
            "properties": {
                "channel": {"type": "string", "description": "Channel ID or name to send to"},
                "text": {"type": "string", "description": "Message text content"},
            },
        },
    },
    {
        "server": "slack",
        "name": "list_channels",
        "description": "List all channels in the Slack workspace with their metadata and topic information.",
        "inputSchema": {
            "properties": {
                "limit": {"type": "integer", "description": "Maximum channels to return"},
            },
        },
    },
    {
        "server": "slack",
        "name": "list_users",
        "description": "List all users in the Slack workspace including profile info and status.",
    },
    {
        "server": "slack",
        "name": "get_channel_history",
        "description": "Get recent messages from a Slack channel with pagination support.",
        "inputSchema": {
            "properties": {
                "channel": {"type": "string", "description": "Channel ID"},
                "limit": {"type": "integer", "description": "Number of messages to retrieve"},
            },
        },
    },
    {
        "server": "slack",
        "name": "add_reaction",
        "description": "Add an emoji reaction to a Slack message in a channel.",
        "inputSchema": {
            "properties": {
                "channel": {"type": "string", "description": "Channel containing the message"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
                "reaction": {"type": "string", "description": "Emoji name without colons (e.g., 'thumbsup')"},
            },
        },
    },
    # ── postgres ────────────────────────────────────────────────
    {
        "server": "postgres",
        "name": "query",
        "description": "Run a SQL query against the PostgreSQL database and return the results as rows.",
        "inputSchema": {
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute"},
                "params": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional query parameters for parameterized queries",
                },
            },
        },
    },
    {
        "server": "postgres",
        "name": "list_tables",
        "description": "List all tables in the PostgreSQL database with their schema names and row counts.",
    },
    {
        "server": "postgres",
        "name": "describe_table",
        "description": "Get the schema of a specific table including column names, types, and constraints.",
        "inputSchema": {
            "properties": {
                "table": {"type": "string", "description": "Table name to describe"},
            },
        },
    },
    {
        "server": "postgres",
        "name": "insert",
        "description": "Insert rows into a PostgreSQL table with column-value mapping.",
        "inputSchema": {
            "properties": {
                "table": {"type": "string", "description": "Target table name"},
                "data": {"type": "object", "description": "Column-value pairs to insert"},
            },
        },
    },
    {
        "server": "postgres",
        "name": "update",
        "description": "Update rows in a PostgreSQL table with SET conditions and WHERE clause.",
        "inputSchema": {
            "properties": {
                "table": {"type": "string", "description": "Target table name"},
                "set": {"type": "object", "description": "Column-value pairs to set"},
                "where": {"type": "string", "description": "WHERE clause conditions"},
            },
        },
    },
    # ── weather ─────────────────────────────────────────────────
    {
        "server": "weather",
        "name": "get_forecast",
        "description": "Get weather forecast for a location. Returns temperature, humidity, precipitation, and conditions.",
        "inputSchema": {
            "properties": {
                "latitude": {"type": "number", "description": "Latitude coordinate"},
                "longitude": {"type": "number", "description": "Longitude coordinate"},
            },
        },
    },
    {
        "server": "weather",
        "name": "get_alerts",
        "description": "Get severe weather alerts for a region. Returns active warnings, watches, and advisories.",
        "inputSchema": {
            "properties": {
                "region": {"type": "string", "description": "Region code or state abbreviation"},
            },
        },
    },
    {
        "server": "weather",
        "name": "get_current_weather",
        "description": "Get current weather conditions for a location including temperature, wind, and humidity.",
        "inputSchema": {
            "properties": {
                "latitude": {"type": "number", "description": "Latitude coordinate"},
                "longitude": {"type": "number", "description": "Longitude coordinate"},
            },
        },
    },
    # ── fetch ───────────────────────────────────────────────────
    {
        "server": "fetch",
        "name": "fetch_url",
        "description": "Fetch a URL and return its content as raw text. Supports HTTP and HTTPS.",
        "inputSchema": {
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
        },
    },
    {
        "server": "fetch",
        "name": "fetch_markdown",
        "description": "Fetch a URL and convert the content to markdown format using readability algorithms.",
        "inputSchema": {
            "properties": {
                "url": {"type": "string", "description": "URL to fetch and convert"},
            },
        },
    },
    # ── notion ──────────────────────────────────────────────────
    {
        "server": "notion",
        "name": "create_page",
        "description": "Create a new Notion page in a database or as a standalone page with content blocks.",
        "inputSchema": {
            "properties": {
                "parent_id": {"type": "string", "description": "Parent page or database ID"},
                "title": {"type": "string", "description": "Page title"},
                "content": {"type": "array", "description": "Array of content blocks for the page"},
            },
        },
    },
    {
        "server": "notion",
        "name": "update_page",
        "description": "Update an existing Notion page's properties and content blocks.",
        "inputSchema": {
            "properties": {
                "page_id": {"type": "string", "description": "Page ID to update"},
                "properties": {"type": "object", "description": "Page properties to update"},
            },
        },
    },
    {
        "server": "notion",
        "name": "search_pages",
        "description": "Search Notion pages by title or content using full-text search across the workspace.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Search query text"},
                "limit": {"type": "integer", "description": "Maximum number of results"},
            },
        },
    },
    {
        "server": "notion",
        "name": "get_page",
        "description": "Retrieve a Notion page by its ID, including all properties and content.",
        "inputSchema": {
            "properties": {
                "page_id": {"type": "string", "description": "Page ID to retrieve"},
            },
        },
    },
    {
        "server": "notion",
        "name": "create_database",
        "description": "Create a new Notion database with custom property schema and views.",
        "inputSchema": {
            "properties": {
                "parent_id": {"type": "string", "description": "Parent page ID"},
                "title": {"type": "string", "description": "Database title"},
                "properties": {"type": "object", "description": "Database property schema definitions"},
            },
        },
    },
    {
        "server": "notion",
        "name": "query_database",
        "description": "Query a Notion database with filters and sorting to retrieve matching entries.",
        "inputSchema": {
            "properties": {
                "database_id": {"type": "string", "description": "Database ID to query"},
                "filter": {"type": "object", "description": "Filter conditions for the query"},
            },
        },
    },
    # ── gmail ───────────────────────────────────────────────────
    {
        "server": "gmail",
        "name": "send_email",
        "description": "Send an email via Gmail with subject, body, and recipients. Supports attachments.",
        "inputSchema": {
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body content"},
            },
        },
    },
    {
        "server": "gmail",
        "name": "search_emails",
        "description": "Search Gmail messages by query with optional filters like date range and labels.",
        "inputSchema": {
            "properties": {
                "query": {"type": "string", "description": "Gmail search query (e.g., 'from:alice is:unread')"},
                "max_results": {"type": "integer", "description": "Maximum messages to return"},
            },
        },
    },
    {
        "server": "gmail",
        "name": "read_email",
        "description": "Read a specific email by its ID. Returns full message content with headers and body.",
        "inputSchema": {
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID to read"},
            },
        },
    },
    {
        "server": "gmail",
        "name": "list_labels",
        "description": "List all Gmail labels for the authenticated account with their metadata.",
    },
    {
        "server": "gmail",
        "name": "create_draft",
        "description": "Create a draft email that can be reviewed and sent later.",
        "inputSchema": {
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body content"},
            },
        },
    },
    # ── calendar ────────────────────────────────────────────────
    {
        "server": "calendar",
        "name": "create_event",
        "description": "Create a calendar event with title, time, attendees, and optional reminders.",
        "inputSchema": {
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "Event start time in ISO 8601 format"},
                "end_time": {"type": "string", "description": "Event end time in ISO 8601 format"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee email addresses"},
            },
        },
    },
    {
        "server": "calendar",
        "name": "list_events",
        "description": "List upcoming calendar events within a date range with optional filters.",
        "inputSchema": {
            "properties": {
                "time_min": {"type": "string", "description": "Start of time range (ISO 8601)"},
                "time_max": {"type": "string", "description": "End of time range (ISO 8601)"},
                "max_results": {"type": "integer", "description": "Maximum events to return"},
            },
        },
    },
    {
        "server": "calendar",
        "name": "delete_event",
        "description": "Delete a calendar event by its event ID. Cannot be undone.",
        "inputSchema": {
            "properties": {
                "event_id": {"type": "string", "description": "Event ID to delete"},
            },
        },
    },
    {
        "server": "calendar",
        "name": "find_available_slots",
        "description": "Find available time slots across calendars within a date range and duration.",
        "inputSchema": {
            "properties": {
                "date": {"type": "string", "description": "Date to check (YYYY-MM-DD)"},
                "duration_minutes": {"type": "integer", "description": "Required duration in minutes"},
            },
        },
    },
    {
        "server": "calendar",
        "name": "update_event",
        "description": "Update an existing calendar event's details such as time, title, or attendees.",
        "inputSchema": {
            "properties": {
                "event_id": {"type": "string", "description": "Event ID to update"},
                "summary": {"type": "string", "description": "Updated event title"},
            },
        },
    },
    # ── jira ────────────────────────────────────────────────────
    {
        "server": "jira",
        "name": "create_issue_jira",
        "description": "Create a new Jira issue in a project with summary, description, and type.",
        "inputSchema": {
            "properties": {
                "project": {"type": "string", "description": "Project key (e.g., 'PROJ')"},
                "summary": {"type": "string", "description": "Issue summary/title"},
                "description": {"type": "string", "description": "Issue description"},
                "issuetype": {"type": "string", "description": "Issue type (e.g., 'Bug', 'Task')"},
            },
        },
    },
    {
        "server": "jira",
        "name": "search_issues",
        "description": "Search Jira issues by JQL query string with optional field filtering.",
        "inputSchema": {
            "properties": {
                "jql": {"type": "string", "description": "JQL query string (e.g., 'project = PROJ AND status = Open')"},
                "max_results": {"type": "integer", "description": "Maximum results to return"},
            },
        },
    },
    {
        "server": "jira",
        "name": "get_issue_jira",
        "description": "Get Jira issue details by issue key including all fields and comments.",
        "inputSchema": {
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key (e.g., 'PROJ-123')"},
            },
        },
    },
    {
        "server": "jira",
        "name": "add_comment_jira",
        "description": "Add a comment to a Jira issue with formatted text content.",
        "inputSchema": {
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key to comment on"},
                "body": {"type": "string", "description": "Comment body text"},
            },
        },
    },
    {
        "server": "jira",
        "name": "transition_issue",
        "description": "Transition a Jira issue to a new status (e.g., 'In Progress', 'Done').",
        "inputSchema": {
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key to transition"},
                "transition_name": {"type": "string", "description": "Target transition name"},
            },
        },
    },
    # ── aws-s3 ──────────────────────────────────────────────────
    {
        "server": "aws-s3",
        "name": "list_buckets",
        "description": "List all S3 buckets in the AWS account with their creation dates and regions.",
    },
    {
        "server": "aws-s3",
        "name": "list_objects",
        "description": "List objects in an S3 bucket with optional prefix and delimiter filtering.",
        "inputSchema": {
            "properties": {
                "bucket": {"type": "string", "description": "S3 bucket name"},
                "prefix": {"type": "string", "description": "Object key prefix to filter by"},
            },
        },
    },
    {
        "server": "aws-s3",
        "name": "upload_file",
        "description": "Upload a file to an S3 bucket with configurable content type and permissions.",
        "inputSchema": {
            "properties": {
                "bucket": {"type": "string", "description": "Target bucket name"},
                "key": {"type": "string", "description": "Object key/path in the bucket"},
                "file_path": {"type": "string", "description": "Local file path to upload"},
            },
        },
    },
    {
        "server": "aws-s3",
        "name": "download_file",
        "description": "Download a file from an S3 bucket to a local path on disk.",
        "inputSchema": {
            "properties": {
                "bucket": {"type": "string", "description": "Source bucket name"},
                "key": {"type": "string", "description": "Object key to download"},
                "file_path": {"type": "string", "description": "Local destination path"},
            },
        },
    },
    {
        "server": "aws-s3",
        "name": "delete_object",
        "description": "Delete an object from an S3 bucket permanently.",
        "inputSchema": {
            "properties": {
                "bucket": {"type": "string", "description": "Bucket containing the object"},
                "key": {"type": "string", "description": "Object key to delete"},
            },
        },
    },
    # ── redis ───────────────────────────────────────────────────
    {
        "server": "redis",
        "name": "get_key",
        "description": "Get the value of a Redis key by its name. Returns the stored value or null if not found.",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Redis key name"},
            },
        },
    },
    {
        "server": "redis",
        "name": "set_key",
        "description": "Set a Redis key with an optional TTL (time-to-live) expiration in seconds.",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Redis key name"},
                "value": {"type": "string", "description": "Value to store"},
                "ttl": {"type": "integer", "description": "Optional TTL in seconds"},
            },
        },
    },
    {
        "server": "redis",
        "name": "delete_key",
        "description": "Delete a Redis key permanently from the database.",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Redis key name to delete"},
            },
        },
    },
    {
        "server": "redis",
        "name": "scan_keys",
        "description": "Scan Redis keys by glob pattern using the SCAN command for non-blocking iteration.",
        "inputSchema": {
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g., 'user:*')"},
                "count": {"type": "integer", "description": "Approximate number of keys to return per iteration"},
            },
        },
    },
    {
        "server": "redis",
        "name": "get_type",
        "description": "Get the data type of a Redis key (string, list, set, hash, zset, etc.).",
        "inputSchema": {
            "properties": {
                "key": {"type": "string", "description": "Redis key name to check"},
            },
        },
    },
]

# ═══════════════════════════════════════════════════════════════════
# 2.  TEST QUERIES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class QueryCase:
    query: str
    expected: set[str]  # Set of acceptable tool names (top-1 match)
    note: str = ""


QUERIES: list[QueryCase] = [
    # ── Direct / unambiguous ────────────────────────────────────
    QueryCase("search the web for information about climate change", {"brave_web_search"}),
    QueryCase("take a screenshot of this webpage", {"puppeteer_screenshot"}),
    QueryCase("what do I remember about the project deadline", {"memory_search"}),
    QueryCase("remember this fact for later", {"memory_create"}),
    QueryCase("read the contents of a file", {"read_file"}),
    QueryCase("create a new GitHub issue about the bug", {"create_issue"}),
    QueryCase("navigate to a URL in the browser", {"puppeteer_navigate"}),
    QueryCase("what's the weather forecast for tomorrow", {"get_forecast"}),
    QueryCase("send a message to the team channel", {"send_message"}),
    QueryCase("search for files matching a pattern", {"search_files"}),
    QueryCase("run a SQL query against the database", {"query"}),
    QueryCase("what channels are available in slack", {"list_channels"}),
    QueryCase("fetch the content of a web page", {"fetch_url"}),
    QueryCase("delete a file from disk", {"delete_file"}),
    QueryCase("list all open issues in the repo", {"list_issues"}),
    QueryCase("update my current context with new information", {"update_context"}),
    QueryCase("click a button on the page", {"puppeteer_click"}),
    QueryCase("get current weather in Tokyo", {"get_current_weather"}),
    QueryCase("what tables are in the database", {"list_tables"}),
    QueryCase("execute JavaScript code in the browser", {"puppeteer_evaluate"}),
    # ── THE PROBLEM CASE ─────────────────────────────────────────
    QueryCase(
        "memory context agent state",
        {"get_context"},
        note="PROBLEM CASE: ambiguous terms that could match multiple nous tools",
    ),
    # ── Additional direct queries ────────────────────────────────
    QueryCase("write some data to a file on disk", {"write_file"}),
    QueryCase("list the contents of a directory", {"list_directory"}),
    QueryCase("create a new directory", {"create_directory"}),
    QueryCase("move a file to another location", {"move_file"}),
    QueryCase("get metadata about a file", {"get_file_info"}),
    QueryCase("search github for python projects", {"search_repos"}),
    QueryCase("create a pull request on github", {"create_pr"}),
    QueryCase("list pull requests in a repository", {"list_prs"}),
    QueryCase("get details of github issue number 42", {"get_issue"}),
    QueryCase("add a comment to an issue", {"add_comment"}),
    QueryCase("get the contents of a file from github", {"get_file_contents"}),
    QueryCase("search for local businesses near me", {"brave_local_search"}),
    QueryCase("fill in a form on the page", {"puppeteer_fill"}),
    QueryCase("insert data into a database table", {"insert"}),
    QueryCase("update records in the database", {"update"}),
    QueryCase("get the schema of a table", {"describe_table"}),
    QueryCase("get severe weather alerts for California", {"get_alerts"}),
    QueryCase("fetch a URL and convert to markdown", {"fetch_markdown"}),
    QueryCase("list all users in the slack workspace", {"list_users"}),
    QueryCase("get the message history of a channel", {"get_channel_history"}),
    QueryCase("add an emoji reaction to a message", {"add_reaction"}),
    QueryCase("clear the current session context", {"context_clear"}),
    QueryCase("append data to the current context", {"context_append"}),
    QueryCase("remove a memory entry by its key", {"memory_delete"}),
    # ── New tools queries ───────────────────────────────────────
    QueryCase("create a new notion page", {"create_page"}),
    QueryCase("search notion pages for meeting notes", {"search_pages"}),
    QueryCase("query a notion database with filters", {"query_database"}),
    QueryCase("send an email to a colleague", {"send_email"}),
    QueryCase("search my gmail inbox for invoices", {"search_emails"}),
    QueryCase("read a specific email message", {"read_email"}),
    QueryCase("list all gmail labels", {"list_labels"}),
    QueryCase("create a draft email for later", {"create_draft"}),
    QueryCase("schedule a meeting for next week", {"create_event"}),
    QueryCase("show my upcoming calendar events", {"list_events"}),
    QueryCase("delete a calendar event", {"delete_event"}),
    QueryCase("find available time slots tomorrow", {"find_available_slots"}),
    QueryCase("create a new jira bug ticket", {"create_issue_jira"}),
    QueryCase("search jira issues by project", {"search_issues"}),
    QueryCase("get details of jira ticket ABC-123", {"get_issue_jira"}),
    QueryCase("add a comment to a jira issue", {"add_comment_jira"}),
    QueryCase("move a jira ticket to done", {"transition_issue"}),
    QueryCase("list all s3 buckets", {"list_buckets"}),
    QueryCase("list objects in an s3 bucket", {"list_objects"}),
    QueryCase("upload a file to s3", {"upload_file"}),
    QueryCase("download a file from s3", {"download_file"}),
    QueryCase("delete an object from an s3 bucket", {"delete_object"}),
    QueryCase("get the value of a redis key", {"get_key"}),
    QueryCase("set a redis key with expiration", {"set_key"}),
    QueryCase("delete a redis key", {"delete_key"}),
    QueryCase("scan redis keys by pattern", {"scan_keys"}),
    QueryCase("check the type of a redis key", {"get_type"}),
    # ── Ambiguous queries ───────────────────────────────────────
    QueryCase(
        "search",
        {"search_repos", "search_files", "brave_web_search", "memory_search", "search_pages", "search_emails", "search_issues"},
        note="Very broad — many servers have search tools",
    ),
    QueryCase(
        "file contents",
        {"read_file", "get_file_contents"},
        note="Ambiguous: local disk vs GitHub",
    ),
    QueryCase(
        "get the weather",
        {"get_current_weather", "get_forecast"},
        note="Could be current or forecast",
    ),
    QueryCase(
        "github pull request",
        {"create_pr", "list_prs"},
        note="Ambiguous verb: create or list?",
    ),
    # ── Edge case queries ───────────────────────────────────────
    QueryCase(
        "read",
        {"read_file"},
        note="Very short query — single word",
    ),
    QueryCase(
        "I need to find out what the weather is going to be like tomorrow so I can plan my outdoor activities accordingly",
        {"get_forecast"},
        note="Long verbose query",
    ),
    QueryCase(
        "screnshot of webpage",
        {"puppeteer_screenshot"},
        note="Misspelled query",
    ),
    # ── Ambiguous broad ─────────────────────────────────────────
    QueryCase(
        "get information",
        {"get_file_info", "get_issue", "get_issue_jira", "get_page", "get_forecast"},
        note="Broad — many 'get*' tools",
    ),
    QueryCase(
        "create something",
        {"create_issue", "create_directory", "create_page", "create_database", "create_event", "create_pr", "create_draft"},
        note="Broad — many 'create*' tools",
    ),
    QueryCase(
        "list all things",
        {"list_directory", "list_channels", "list_tables", "list_events", "list_issues",
         "list_users", "list_buckets", "list_objects", "list_labels", "list_prs"},
        note="Broad — many 'list*' tools",
    ),
    # ── Semantic queries (BM25 may struggle) ────────────────────
    QueryCase("find by repository name", {"search_repos"}),
    QueryCase("what's happening in the next few days", {"list_events"},
              note="Semantic — no direct keyword overlap"),
    QueryCase("clear my working memory", {"context_clear"},
              note="Semantic — metaphorical phrasing"),
    QueryCase("store this for later recall", {"memory_create"},
              note="Semantic — indirect phrasing"),
    QueryCase("grab that webpage content", {"fetch_url"},
              note="Semantic — casual language"),
    QueryCase("jot down a note", {"create_page"},
              note="Semantic — metaphorical"),
    QueryCase("fire off an email", {"send_email"},
              note="Semantic — informal language"),
    QueryCase("check my inbox", {"search_emails"},
              note="Semantic — 'inbox' is a gmail concept"),
    QueryCase("look up that bug ticket", {"get_issue_jira"},
              note="Semantic — 'bug ticket' is a jira concept"),
    QueryCase("set a reminder for tomorrow", {"create_event"},
              note="Semantic — reminder vs event"),
]


# ═══════════════════════════════════════════════════════════════════
# 3.  ABSTRACT SEARCH APPROACH
# ═══════════════════════════════════════════════════════════════════

class SearchApproach(ABC):
    """Interface all search approaches must implement."""

    name: str
    _available: bool = True  # Some approaches may be unavailable (e.g., fastembed)

    @abstractmethod
    def index(self, tools: list[dict]) -> None:
        """Build index from tool documents."""
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[str]:
        """Return tool names in rank order."""
        ...


# ═══════════════════════════════════════════════════════════════════
# 4.  TOKENIZER  (exact copy from meta_provider.py ToolIndex)
# ═══════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Code-aware tokenizer. Copied from meta_provider.py ToolIndex._tokenize."""
    seen: set[str] = set()
    out: list[str] = []

    def emit(token: str) -> None:
        token = token.strip().lower()
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    for word in text.split():
        # Keep original word (preserves snake_case)
        emit(word)

        # Split on non-alnum
        sub_parts = re.findall(r"[a-zA-Z0-9]+", word)
        for part in sub_parts:
            emit(part)

            # camelCase/PascalCase splitting
            crunched = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
            crunched = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", crunched)
            if crunched != part:
                for camel_piece in crunched.split():
                    emit(camel_piece)

            # Digit boundary
            digit_parts = re.split(r"(\d+)", part)
            if len(digit_parts) > 1:
                for dp in digit_parts:
                    if dp and dp != part:
                        emit(dp)

    return out


def _build_doc_tokens(doc: dict) -> list[str]:
    """Weighted token list for a tool document. Copied from meta_provider.py."""
    tokens: list[str] = []

    def _add(text: str, copies: int = 1) -> None:
        field_tokens = _tokenize(text)
        for _ in range(copies):
            tokens.extend(field_tokens)

    _add(doc["name"], copies=5)
    _add(doc["server"], copies=3)
    _add(doc.get("description", ""), copies=2)

    schema = doc.get("inputSchema", {})
    if isinstance(schema, dict):
        for param_name, param_info in schema.get("properties", {}).items():
            _add(param_name, copies=1)
            if isinstance(param_info, dict):
                _add(param_info.get("type", ""), copies=1)
                _add(param_info.get("description", ""), copies=1)
                for ev in param_info.get("enum", []):
                    if isinstance(ev, str):
                        _add(ev, copies=2)
    return tokens


def _build_doc_tokens_flat(doc: dict) -> list[str]:
    """Flat (unweighted) token list — tokenize each field once."""
    tokens: list[str] = []

    def _add(text: str) -> None:
        tokens.extend(_tokenize(text))

    _add(doc["name"])
    _add(doc["server"])
    _add(doc.get("description", ""))

    schema = doc.get("inputSchema", {})
    if isinstance(schema, dict):
        for param_name, param_info in schema.get("properties", {}).items():
            _add(param_name)
            if isinstance(param_info, dict):
                _add(param_info.get("type", ""))
                _add(param_info.get("description", ""))
                for ev in param_info.get("enum", []):
                    if isinstance(ev, str):
                        _add(ev)
    return tokens


def _stem_tokens(tokens: list[str]) -> list[str]:
    """Apply English stemming to a list of tokens."""
    if _STEMMER is None:
        return tokens
    stemmed: list[str] = []
    for t in tokens:
        try:
            stemmed.append(_STEMMER.stem_word(t))
        except Exception:
            stemmed.append(t)
    return stemmed


def _build_doc_tokens_stemmed(doc: dict) -> list[str]:
    """Token list with stemming applied after standard tokenization + weighting."""
    raw = _build_doc_tokens(doc)
    return _stem_tokens(raw)


def _build_doc_tokens_flat_stemmed(doc: dict) -> list[str]:
    """Flat (unweighted) token list with stemming."""
    raw = _build_doc_tokens_flat(doc)
    return _stem_tokens(raw)


# Initialize stemmer once
_STEMMER: Any = None
try:
    from py_rust_stemmers import SnowballStemmer
    _STEMMER = SnowballStemmer("english")
except Exception:
    log.info("  [stemming] py_rust_stemmers not available — stemming will be disabled")
    _STEMMER = None


# ═══════════════════════════════════════════════════════════════════
# 5.  APPROACH 1:  BM25 (baseline)
# ═══════════════════════════════════════════════════════════════════

class BM25Approach(SearchApproach):
    name = "BM25 (baseline)"

    def __init__(self) -> None:
        from rank_bm25 import BM25Okapi
        self._BM25Okapi = BM25Okapi
        self._documents: list[dict] = []
        self._model: Any = None
        self._corpus: list[list[str]] = []

    def index(self, tools: list[dict]) -> None:
        self._documents = tools
        self._corpus = [_build_doc_tokens(d) for d in tools]
        self._model = self._BM25Okapi(self._corpus) if self._corpus else None
        log.info("  [BM25] Indexed %d tools", len(tools))

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._model or not self._corpus:
            return []
        tokens = _tokenize(query)
        return self._search_tokens(tokens, top_k)

    def _search_tokens(self, tokens: list[str], top_k: int = 5) -> list[str]:
        """Search using pre-tokenized query. Used by query expansion approaches."""
        if not self._model or not self._corpus:
            return []
        scores = self._model.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[str] = []
        for idx in ranked:
            if scores[idx] <= 0:
                break
            if len(results) >= top_k:
                break
            results.append(self._documents[idx]["name"])
        return results


# ═══════════════════════════════════════════════════════════════════
# 5b. APPROACH 1b: BM25 (flat) — no field weighting
# ═══════════════════════════════════════════════════════════════════

class BM25Flat(BM25Approach):
    name = "BM25 (flat)"

    def index(self, tools: list[dict]) -> None:
        self._documents = tools
        self._corpus = [_build_doc_tokens_flat(d) for d in tools]
        self._model = self._BM25Okapi(self._corpus) if self._corpus else None
        log.info("  [BM25 flat] Indexed %d tools", len(tools))


# ═══════════════════════════════════════════════════════════════════
# 5c. APPROACH 1c: BM25 + stemming
# ═══════════════════════════════════════════════════════════════════

class BM25Stemming(BM25Approach):
    name = "BM25 + stemming"

    def __init__(self) -> None:
        super().__init__()
        self._stemmer_available = _STEMMER is not None

    def index(self, tools: list[dict]) -> None:
        if not self._stemmer_available:
            log.warning("  [BM25+stemming] Stemmer unavailable — falling back to plain BM25")
            super().index(tools)
            return
        self._documents = tools
        self._corpus = [_build_doc_tokens_stemmed(d) for d in tools]
        self._model = self._BM25Okapi(self._corpus) if self._corpus else None
        log.info("  [BM25+stemming] Indexed %d tools", len(tools))

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._stemmer_available:
            return super().search(query, top_k)
        if not self._model or not self._corpus:
            return []
        tokens = _stem_tokens(_tokenize(query))
        return self._search_tokens(tokens, top_k)


# ═══════════════════════════════════════════════════════════════════
# 6.  APPROACH 2:  fastembed
# ═══════════════════════════════════════════════════════════════════

class FastembedApproach(SearchApproach):
    name = "fastembed"

    def __init__(self) -> None:
        self._doc_texts: list[str] = []
        self._tool_names: list[str] = []
        self._embeddings: Any = None  # np.ndarray after indexing
        self._model: Any = None
        self._available = False

    def _check_available(self) -> bool:
        if self._available:
            return True
        try:
            from fastembed import TextEmbedding  # noqa: F811
            self._model = TextEmbedding("BAAI/bge-small-en-v1.5")
            self._available = True
            return True
        except Exception as exc:
            log.warning("  [fastembed] SKIPPED — could not load model: %s", exc)
            return False

    def _make_doc_text(self, tool: dict) -> str:
        """Override in subclasses to change doc representation."""
        return f"{tool['server']}/{tool['name']}: {tool['description']}"

    def index(self, tools: list[dict]) -> None:
        if not self._check_available():
            return
        import numpy as np
        self._doc_texts = [self._make_doc_text(t) for t in tools]
        self._tool_names = [t["name"] for t in tools]
        # fastembed returns a generator of numpy arrays
        embeddings_list = list(self._model.embed(self._doc_texts))
        self._embeddings = np.array([emb for emb in embeddings_list])
        # Normalize
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._embeddings = self._embeddings / norms
        log.info("  [fastembed] Indexed %d tools (dim=%d)", len(tools), self._embeddings.shape[1])

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._available or self._embeddings is None:
            return []
        import numpy as np
        query_vec = np.array(list(self._model.embed([query]))[0])
        q_norm = np.linalg.norm(query_vec)
        if q_norm > 0:
            query_vec = query_vec / q_norm
        scores = np.dot(self._embeddings, query_vec)
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [self._tool_names[i] for i in top_indices]


# ═══════════════════════════════════════════════════════════════════
# 6b. APPROACH 2b: fastembed (compact) — no server prefix
# ═══════════════════════════════════════════════════════════════════

class FastembedCompact(FastembedApproach):
    name = "fastembed (compact)"

    def _make_doc_text(self, tool: dict) -> str:
        return f"{tool['name']}: {tool['description']}"


# ═══════════════════════════════════════════════════════════════════
# 7.  APPROACH 3:  BM25 + fastembed RRF hybrid
# ═══════════════════════════════════════════════════════════════════

class BM25FastembedRRF(SearchApproach):
    name = "BM25 + fastembed RRF"

    def __init__(self) -> None:
        self._bm25 = BM25Approach()
        self._embed = FastembedApproach()
        self._tool_names: list[str] = []

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._bm25.index(tools)
        self._embed.index(tools)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        bm25_results = self._bm25.search(query, top_k=len(self._tool_names))
        embed_results = self._embed.search(query, top_k=len(self._tool_names))

        # RRF fusion with k=60
        K = 60
        scores: dict[str, float] = {}
        for rank, name in enumerate(bm25_results, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (K + rank)
        for rank, name in enumerate(embed_results, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (K + rank)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in ranked[:top_k]]


# ═══════════════════════════════════════════════════════════════════
# 8.  APPROACH 4:  BM25 retrieval + fastembed re-rank
# ═══════════════════════════════════════════════════════════════════

class BM25FastembedRerank(SearchApproach):
    name = "BM25 + fastembed re-rank"

    def __init__(self) -> None:
        self._bm25 = BM25Approach()
        self._embed = FastembedApproach()
        self._doc_texts: list[str] = []
        self._tool_names: list[str] = []

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._doc_texts = [f"{t['server']}/{t['name']}: {t['description']}" for t in tools]
        self._bm25.index(tools)
        self._embed.index(tools)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._embed._available:
            return self._bm25.search(query, top_k=top_k)

        # BM25 retrieves top 20 candidates
        candidates = self._bm25.search(query, top_k=20)
        if not candidates:
            return []

        import numpy as np
        query_vec = np.array(list(self._embed._model.embed([query]))[0])
        q_norm = np.linalg.norm(query_vec)
        if q_norm > 0:
            query_vec = query_vec / q_norm

        # Get embeddings for candidates only
        candidate_indices = [self._tool_names.index(c) for c in candidates]
        candidate_embs = np.array([list(self._embed._model.embed([self._doc_texts[i]]))[0] for i in candidate_indices])
        norms = np.linalg.norm(candidate_embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        candidate_embs = candidate_embs / norms

        scores = np.dot(candidate_embs, query_vec)
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [candidates[i] for i in top_indices]


# ═══════════════════════════════════════════════════════════════════
# 8b. APPROACH 4b: BM25 + fastembed weighted score fusion (α=0.5)
# ═══════════════════════════════════════════════════════════════════

class BM25FastembedWeighted(SearchApproach):
    name = "BM25 + fastembed weighted (α=0.5)"

    def __init__(self) -> None:
        self._bm25 = BM25Approach()
        self._embed = FastembedApproach()
        self._tool_names: list[str] = []

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._bm25.index(tools)
        self._embed.index(tools)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._embed._available:
            return self._bm25.search(query, top_k=top_k)
        if not self._bm25._model:
            return []
        import numpy as np

        # BM25 raw scores for all documents
        tokens = _tokenize(query)
        bm25_scores = np.array(self._bm25._model.get_scores(tokens), dtype=np.float64)

        # Embedding cosine similarity for all documents
        query_vec = np.array(list(self._embed._model.embed([query]))[0])
        q_norm = np.linalg.norm(query_vec)
        if q_norm > 0:
            query_vec = query_vec / q_norm
        embed_scores: np.ndarray = np.dot(self._embed._embeddings, query_vec)

        # Min-max normalize BM25 scores
        bm25_min, bm25_max = bm25_scores.min(), bm25_scores.max()
        if bm25_max > bm25_min:
            bm25_norm = (bm25_scores - bm25_min) / (bm25_max - bm25_min)
        else:
            bm25_norm = np.zeros_like(bm25_scores)

        # Min-max normalize embedding scores
        emb_min, emb_max = embed_scores.min(), embed_scores.max()
        if emb_max > emb_min:
            embed_norm = (embed_scores - emb_min) / (emb_max - emb_min)
        else:
            embed_norm = np.zeros_like(embed_scores)

        # Weighted fusion
        alpha = 0.5
        combined = alpha * bm25_norm + (1.0 - alpha) * embed_norm

        top_indices = np.argsort(combined)[::-1][:top_k]
        return [self._tool_names[i] for i in top_indices]


# ═══════════════════════════════════════════════════════════════════
# 8c. APPROACH: BM25 + query expansion (fastembed-based)
# ═══════════════════════════════════════════════════════════════════

class BM25QueryExpansion(SearchApproach):
    name = "BM25 + query expansion"

    def __init__(self) -> None:
        self._bm25 = BM25Approach()
        self._embed = FastembedApproach()
        self._tool_names: list[str] = []
        self._doc_texts: list[str] = []

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._doc_texts = [f"{t['server']}/{t['name']}: {t['description']}" for t in tools]
        self._bm25.index(tools)
        self._embed.index(tools)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if not self._embed._available:
            return self._bm25.search(query, top_k=top_k)
        if not self._bm25._model:
            return []

        # Step 1: Tokenize the original query
        query_tokens = _tokenize(query)

        # Step 2: Find top-3 semantically similar tool descriptions
        related = self._embed.search(query, top_k=3)
        if not related:
            return self._bm25.search(query, top_k=top_k)

        # Step 3: Extract tokens from those descriptions
        expansion_tokens: list[str] = []
        seen_expansion: set[str] = set()
        for name in related:
            idx = self._tool_names.index(name)
            text = self._doc_texts[idx]
            for tok in _tokenize(text):
                if tok not in seen_expansion:
                    seen_expansion.add(tok)
                    expansion_tokens.append(tok)

        # Step 4: Combine original + expansion tokens
        combined = query_tokens + expansion_tokens

        # Step 5: Run BM25 with expanded token list
        return self._bm25._search_tokens(combined, top_k)


# ═══════════════════════════════════════════════════════════════════
# 9.  APPROACH 5:  TF-IDF (sklearn)
# ═══════════════════════════════════════════════════════════════════

class TfidfApproach(SearchApproach):
    name = "TF-IDF"

    def __init__(self) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer
        self._vectorizer = TfidfVectorizer(
            analyzer="word",
            token_pattern=r"(?u)\b\w+\b",
            ngram_range=(1, 2),
        )
        self._doc_texts: list[str] = []
        self._tool_names: list[str] = []
        self._tfidf_matrix: Any = None

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._doc_texts = [
            f"{t['name']} {t['description']} {t.get('server', '')}"
            for t in tools
        ]
        self._tfidf_matrix = self._vectorizer.fit_transform(self._doc_texts)
        log.info("  [TF-IDF] Indexed %d tools (vocab=%d)", len(tools), self._tfidf_matrix.shape[1])

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if self._tfidf_matrix is None:
            return []
        import numpy as np
        query_vec: Any = self._vectorizer.transform([query])  # scipy sparse matrix
        scores = np.asarray(query_vec.dot(self._tfidf_matrix.T).toarray().flatten())
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [self._tool_names[i] for i in top_indices if scores[i] > 0]


# ═══════════════════════════════════════════════════════════════════
# 10. APPROACH 6:  BM25 + TF-IDF RRF hybrid
# ═══════════════════════════════════════════════════════════════════

class BM25TfidfRRF(SearchApproach):
    name = "BM25 + TF-IDF RRF"

    def __init__(self) -> None:
        self._bm25 = BM25Approach()
        self._tfidf = TfidfApproach()
        self._tool_names: list[str] = []

    def index(self, tools: list[dict]) -> None:
        self._tool_names = [t["name"] for t in tools]
        self._bm25.index(tools)
        self._tfidf.index(tools)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        bm25_results = self._bm25.search(query, top_k=len(self._tool_names))
        tfidf_results = self._tfidf.search(query, top_k=len(self._tool_names))

        K = 60
        scores: dict[str, float] = {}
        for rank, name in enumerate(bm25_results, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (K + rank)
        for rank, name in enumerate(tfidf_results, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (K + rank)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in ranked[:top_k]]


# ═══════════════════════════════════════════════════════════════════
# 11. METRICS & EVALUATION
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Metrics:
    p1: float = 0.0   # Precision@1
    p3: float = 0.0   # Precision@3
    r5: float = 0.0   # Recall@5
    mrr: float = 0.0  # Mean Reciprocal Rank


@dataclass
class QueryDetail:
    query: str
    expected: set[str]
    top1: str | None
    correct: bool
    top5: list[str]


def evaluate(approach: SearchApproach, queries: list[QueryCase]) -> Metrics:
    """Run all queries against an approach and compute metrics."""
    total = len(queries)
    p1_correct = 0
    p3_total = 0.0
    r5_total = 0.0
    rr_total = 0.0

    for q in queries:
        results = approach.search(q.query, top_k=5)
        expected = q.expected

        # Precision@1
        if results and results[0] in expected:
            p1_correct += 1

        # Precision@3 (standard: divide by 3 always; missing slots count as 0)
        top3 = results[:3]
        p3_total += sum(1 for t in top3 if t in expected) / 3.0

        # Recall@5
        if expected:
            found = sum(1 for r in results if r in expected)
            r5_total += found / len(expected)
        else:
            r5_total += 0.0

        # Reciprocal Rank
        rank = 0
        for i, r in enumerate(results, start=1):
            if r in expected:
                rank = i
                break
        if rank > 0:
            rr_total += 1.0 / rank

    return Metrics(
        p1=p1_correct / total,
        p3=p3_total / total,
        r5=r5_total / total,
        mrr=rr_total / total,
    )


def evaluate_detailed(approach: SearchApproach, queries: list[QueryCase]) -> list[QueryDetail]:
    """Run all queries and return per-query details."""
    details: list[QueryDetail] = []
    for q in queries:
        top5 = approach.search(q.query, top_k=5)
        top1 = top5[0] if top5 else None
        details.append(QueryDetail(
            query=q.query,
            expected=q.expected,
            top1=top1,
            correct=top1 is not None and top1 in q.expected,
            top5=top5,
        ))
    return details


# ═══════════════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════════════

def print_table(results: dict[str, Metrics]) -> None:
    """Print comparison table."""
    header = f"{'Approach':<36} {'P@1':>7} {'P@3':>7} {'Recall@5':>9} {'MRR':>7}"
    print()
    print(header)
    print("─" * len(header))
    for name, m in results.items():
        print(f"{name:<36} {m.p1:>7.3f} {m.p3:>7.3f} {m.r5:>9.3f} {m.mrr:>7.3f}")
    print()


def print_problem_case(approaches: list[SearchApproach], queries: list[QueryCase]) -> None:
    """Print per-approach top-1 result for the 'memory context agent state' query."""
    problem = next((q for q in queries if "memory context agent state" in q.query), None)
    if problem is None:
        return
    print(f"\n── Problem case: {problem.query!r} (expected: {problem.expected}) ──")
    for app in approaches:
        results = app.search(problem.query, top_k=3)
        match = "✓" if results and results[0] in problem.expected else "✗"
        print(f"  {app.name:<36} → {results[:3]}  {match}")
    print()


def print_comparison_sections(approaches: list[SearchApproach], queries: list[QueryCase]) -> None:
    """Print per-query comparison sections: BM25 vs fastembed vs RRF."""
    # Find the specific approaches we need
    bm25 = next((a for a in approaches if a.name == "BM25 (baseline)"), None)
    embed = next((a for a in approaches if a.name == "fastembed"), None)
    rrf = next((a for a in approaches if a.name == "BM25 + fastembed RRF"), None)
    weighted = next((a for a in approaches if a.name == "BM25 + fastembed weighted (α=0.5)"), None)

    if not bm25 or not embed:
        return

    bm25_details = evaluate_detailed(bm25, queries)
    embed_details = evaluate_detailed(embed, queries) if embed._available else None
    rrf_details = evaluate_detailed(rrf, queries) if rrf and embed and embed._available else None
    weighted_details = evaluate_detailed(weighted, queries) if weighted and embed and embed._available else None

    if embed_details is None:
        return

    # ── BM25 fails, fastembed wins ──
    print("── BM25 fails, fastembed wins ──")
    count = 0
    for b, e in zip(bm25_details, embed_details):
        if not b.correct and e.correct:
            count += 1
            print(f"  {b.query!r}")
            print(f"    BM25:          {b.top1!r}  (expected: {b.expected})")
            print(f"    fastembed:     {e.top1!r}  ✓")
    if count == 0:
        print("  (none)")
    print()

    # ── fastembed fails, BM25 wins ──
    print("── fastembed fails, BM25 wins ──")
    count = 0
    for b, e in zip(bm25_details, embed_details):
        if b.correct and not e.correct:
            count += 1
            print(f"  {b.query!r}")
            print(f"    BM25:          {b.top1!r}  ✓")
            print(f"    fastembed:     {e.top1!r}  (expected: {e.expected})")
    if count == 0:
        print("  (none)")
    print()

    # ── RRF fixes both ──
    if rrf_details is not None:
        print("── RRF fixes both — where BM25 & fastembed individually fail but RRF succeeds ──")
        count = 0
        for b, e, r in zip(bm25_details, embed_details, rrf_details):
            if not b.correct and not e.correct and r.correct:
                count += 1
                print(f"  {r.query!r}")
                print(f"    BM25:          {b.top1!r}")
                print(f"    fastembed:     {e.top1!r}")
                print(f"    RRF:           {r.top1!r}  ✓ (expected: {r.expected})")
        if count == 0:
            print("  (none)")
        print()

    # ── Weighted fusion fixes both ──
    if weighted_details is not None:
        print("── Weighted fusion fixes both (BM25 & embed fail → weighted wins) ──")
        count = 0
        for b, e, w in zip(bm25_details, embed_details, weighted_details):
            if not b.correct and not e.correct and w.correct:
                count += 1
                print(f"  {w.query!r}")
                print(f"    BM25:          {b.top1!r}")
                print(f"    fastembed:     {e.top1!r}")
                print(f"    weighted(0.5): {w.top1!r}  ✓ (expected: {w.expected})")
        if count == 0:
            print("  (none)")
        print()


# ═══════════════════════════════════════════════════════════════════
# 12. MAIN
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    print("=" * 72)
    print("  MCP Tool Search Approaches Benchmark")
    print("=" * 72)
    print(f"\n  Tools:      {len(TOOLS)}")
    print(f"  Queries:    {len(QUERIES)}")
    print()

    # Indexing phase
    print("── Indexing ──")
    approaches: list[SearchApproach] = [
        BM25Approach(),
        BM25Flat(),
        BM25Stemming(),
        FastembedApproach(),
        FastembedCompact(),
        BM25FastembedRRF(),
        BM25FastembedRerank(),
        BM25FastembedWeighted(),
        BM25QueryExpansion(),
        TfidfApproach(),
        BM25TfidfRRF(),
    ]

    for app in approaches:
        t0 = time.time()
        app.index(TOOLS)
        elapsed = time.time() - t0
        log.info("    → %.2fs", elapsed)

    # Evaluation phase
    print("\n── Evaluating ──")
    results: dict[str, Metrics] = {}
    for app in approaches:
        t0 = time.time()
        metrics = evaluate(app, QUERIES)
        elapsed = time.time() - t0
        results[app.name] = metrics
        log.info("  %-36s done in %.2fs", app.name, elapsed)

    # Output
    print_table(results)
    print_problem_case(approaches, QUERIES)
    print_comparison_sections(approaches, QUERIES)

    # Summary ranking
    print("── Ranking (by MRR) ──")
    ranked = sorted(results.items(), key=lambda x: x[1].mrr, reverse=True)
    for i, (name, m) in enumerate(ranked, start=1):
        print(f"  {i}. {name:<36}  MRR={m.mrr:.3f}  P@1={m.p1:.3f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
