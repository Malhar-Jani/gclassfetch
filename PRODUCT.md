# gclassfetch

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users and purpose

Students collecting professors' Classroom materials for offline study. The user confirmed a simple choose courses → download → review flow with detailed settings tucked away. Finding and organizing materials is the main improvement requested; no particular missing attachment has been identified.

## Operating context

Existing Python/Flask app runs on localhost on Windows, with a command-line mode. It connects directly to Classroom and Drive using the user's Google account. Keep credentials private and preserve existing downloaded files.

## Capabilities and constraints

Read materials, coursework, announcements, linked Drive folders and shortcuts. Export supported Google documents. Organize by course, topic, and post; provide an offline library. Distinguish actual downloads, verified existing files, online resources, and incomplete scans. Owner restrictions and unsupported online content must be explained truthfully.

## Product principles

- One clear primary action for selected courses.
- Help users find course materials without reading technical logs.
- Repeated runs should be safe and should not repeat saved text or links.
- Completion must include a visible account of anything that could not be saved.
