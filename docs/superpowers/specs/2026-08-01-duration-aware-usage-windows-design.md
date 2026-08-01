# Duration-Aware Usage Window Classification

## Context

The Codex usage endpoint historically returned a five-hour usage window as the
primary rate-limit window and a weekly usage window as the secondary window.
The integration therefore maps `primary` directly to its Session sensors and
`secondary` directly to its Weekly sensors.

OpenAI is currently returning some accounts' weekly window in `primary` while
omitting the five-hour window and returning no `secondary` window. The existing
positional mapping consequently exposes weekly usage through both the wrong
sensor name and the wrong dashboard row.

The rate-limit payload includes the full duration of each window through either
`windowDurationMins`, `window_duration_mins`, `window_minutes`, or
`limit_window_seconds`. This provides a more reliable classification signal than
the window's position or its remaining time before reset.

## Goals

- Route a short-term Codex usage window to the existing Session sensors.
- Route a longer-term Codex usage window to the existing Weekly sensors,
  regardless of whether it appears as `primary` or `secondary`.
- Preserve all existing sensor entities so a restored five-hour window begins
  reporting again without a migration or configuration change.
- Remain compatible with older response shapes that omit window duration.
- Keep the change limited to parsing, tests, and concise user-facing
  documentation where necessary.

## Non-goals

- Renaming or removing sensors.
- Creating a new OAuth flow or changing authentication.
- Replacing subscription usage with OpenAI API billing usage.
- Supporting arbitrary named quota windows as new Home Assistant entities.
- Changing dashboard entity IDs or configuration-entry data.

## Design

### Window classification

The parser will inspect both known rate-limit positions: `primary` /
`primary_window` and `secondary` / `secondary_window`. It will classify each
available window before writing sensor data.

Classification uses these signals in order:

1. **Declared full duration.** A duration of 300 minutes or less is a Session
   window. A duration greater than 300 minutes is a Weekly window. The parser
   already normalizes all known duration fields into minutes.
2. **Reset horizon fallback.** If the full duration is unavailable and a window
   resets more than five hours from the parse time, it is a Weekly window.
3. **Legacy positional fallback.** If duration and reset horizon do not identify
   the window, `primary` is treated as Session and `secondary` as Weekly, matching
   existing behavior.

Declared duration takes precedence over reset horizon because a weekly window
can have less than five hours remaining near the end of its cycle.

If two windows classify to the same category, the parser will keep the first
window in API order (`primary`, then `secondary`). This conservative behavior
avoids silently changing a valid primary value in an unexpected payload.

### Sensor population

The existing sensor definitions and unique IDs remain unchanged.

- When a Session window is present, populate `session_usage_percent` and
  `session_reset_time` as today.
- When no Session window is present, omit those keys from coordinator data.
  Home Assistant will therefore mark both existing Session entities
  `Unavailable` through their current `available` implementation.
- When a Weekly window is present, populate `week_usage_percent`,
  `week_reset_time`, and `week_usage_pace`.
- Weekly pace continues to use the selected Weekly window's declared duration.
  If duration is unavailable, pace remains unknown as it does today.

When OpenAI restores a short-term window, the next successful poll will populate
the Session keys and the existing entities will automatically become available.

### Error handling and compatibility

Malformed percentages, reset timestamps, and durations continue to normalize to
`None`; they do not fail the entire coordinator refresh. Existing response-shape
support remains intact.

The fallback order preserves prior positional behavior for old payloads while
correcting the current weekly-in-primary response whenever a reliable duration
or reset horizon is present.

## Tests

Parser tests will cover:

1. A normal 300-minute primary plus 10,080-minute secondary response.
2. A 10,080-minute weekly window in `primary` with no `secondary`, verifying
   that Session keys are absent and Weekly keys are populated.
3. A restored 300-minute primary-only response, verifying Session keys return.
4. Snake-case `primary_window` / `secondary_window` fields using
   `limit_window_seconds`.
5. A duration-less primary window resetting more than five hours away, verifying
   the Weekly fallback.
6. A duration-less ambiguous response, verifying legacy positional behavior.
7. Weekly pace calculation using the window selected by classification.

Tests will use a fixed current time where reset-horizon classification or pace
calculation depends on time, avoiding boundary-sensitive assertions.

## Acceptance criteria

- A weekly-only primary window never populates the Session sensors when its
  duration is greater than five hours.
- Session entities remain registered but become unavailable while the short
  window is absent.
- A later five-hour window is exposed through the same Session entity IDs.
- Existing two-window payloads retain their current sensor values.
- Syntax, lint, and relevant tests pass.
