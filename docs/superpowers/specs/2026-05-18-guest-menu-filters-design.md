# Guest Menu Filters Design

## Goal

Move guest-menu tag visibility out of the primary admin surfaces and make the feature explain itself as a low-frequency control for guest-facing filter chips.

## Current Behavior

- Web: "Tag Management" is shown directly on the home page, even though the only guest-facing behavior is whether a tag appears as a filter on `/menu`.
- iOS: Settings shows every tag and guest-menu visibility toggle inline, making a low-frequency task compete with server, guest menu, and guest cart controls.
- Backend APIs already support the required behavior with `GET /api/tags` and `PUT /api/tags/:id/visibility`.

## Design

- Rename the user-facing feature to "Guest Menu Filters".
- Use copy that explains: guests can use enabled tags to filter the shared menu; this does not hide dishes from the menu.
- Web home page shows a lower-priority row/button that opens a dedicated Guest Menu Filters page.
- iOS Settings shows a navigation row under Guest Menu that opens a dedicated detail screen.
- The dedicated surfaces show each tag, its dish count, and a visibility toggle.
- General tag deletion is not part of this surface.

## Testing

- Run a text-level smoke check that confirms the new web page/route and iOS detail view are present.
- Run `python3 -m py_compile server.py` for backend/server syntax.
- Run an iOS build check if Xcode is available in this environment.
