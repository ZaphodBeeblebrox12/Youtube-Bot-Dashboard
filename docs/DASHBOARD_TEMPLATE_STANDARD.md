# Dashboard Template Standard

**Status:** Canonical visual template — locked  
**Approved:** 25 October 2026  
**Canonical file:** [`docs/reference/approved-dashboard-template.html`](reference/approved-dashboard-template.html)  
**Original user-supplied filename:** `tradethesis_dashboard_static_preview(3).html`  
**Original source SHA-256:** `2caaf45bb3796c0012776fe9e7eda21433f2b1e97d41a908df4979137958befe`

## Binding decision

The user-designated HTML above is the canonical visual reference for the YouTube Bot Dashboard. Preserve that template as-is. This is a locked design baseline, not a suggestion to recreate or reinterpret.

Future functionality and behavior may be added, connected to live data, or improved, but the implementation must continue to follow the canonical template's existing visual structure. Keep its current page hierarchy, top metrics, navigation, main feed/detail layout, lower summary panels, typography, palette, spacing, card styling, labels and responsive behavior unchanged unless the user explicitly approves a design change.

Implement application behavior in the actual runtime (currently `app.py`) rather than modifying the reference HTML to simulate production behavior. Where a feature can be delivered within the existing design, add it without redesigning the template. If a requested feature genuinely requires a visual or structural departure, get the user's explicit approval first.

## Additional locked requirements

- Do not add profile pictures or profile-image storage. Use the initials/monogram treatment, consistent with the user's explicit storage preference.
- Treat example counts, names, messages and member details in the HTML as illustrative sample data only. Never present them as live facts; the runtime must use actual application data and preserve the existing fail-closed rules for filtered messages.
- Preserve the distinct workflow for replies: **Use as reply** loads a draft, the host can edit it, and only a separate **Send** action attempts to publish the edited text.
- Preserve all existing workspace and action semantics unless a requirement change is explicitly approved.
- This HTML file is the design reference, not the deployed Streamlit application or a second runtime.

## Change control

Any change to the canonical template or its visual design requires explicit user approval. Feature additions are welcome only when they preserve the locked visual baseline. Keep this standard and the canonical HTML in `main` so future work can recover this decision without relying on chat history.
