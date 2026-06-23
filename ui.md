# Recipe NL UI Guide

## Source Of Truth

The first home screen reference is `home_page.png`.

The app should feel clean, friendly, practical, and mobile-native. It is a Dutch supermarket and recipe assistant, so the UI should prioritize fast scanning, clear actions, and a soft food/lifestyle mood without becoming a marketing landing page.

## Navigation

- Main app navigation uses a bottom tab bar with five tabs:
  - Home
  - Recepten
  - Boodschappen
  - Planning
  - Profiel
- The tab bar is a floating white rounded rectangle near the bottom of the screen.
- Active tab icon sits in a pale green rounded square.
- Tab labels are short, Dutch, and always visible.
- Stack screens such as import, review, and recipe detail can use a top header.

## Home Layout

Home follows this vertical structure:

1. Large greeting: `Hoi {name}`.
2. Large green primary card: `Recept importeren` with a white circular plus icon.
3. White shortcut card with three equal columns:
   - Plak link
   - Plak tekst
   - Scan recept `#todo`
4. Section title: `Recente recepten`.
5. Horizontal recipe cards with image, title, time, and estimated price per person.

## Colors

Use these app colors from `apps/mobile/lib/theme.ts`:

- Background: `#ffffff`
- Text: `#050505`
- Muted text: `#5f6368`
- Primary green: `#78c883`
- Primary dark green: `#65b772`
- Soft green: `#e2f4e5`
- Active tab green: `#dff2e2`
- Border: `#e5e7eb`
- Danger: `#b91c1c`

Avoid one-note dark palettes, heavy gradients, purple-blue hero treatments, and decorative blobs.

## Typography

- Use system fonts.
- No negative letter spacing.
- Large page titles: 32px, weight 800.
- Section titles: 26px, weight 800.
- Card titles: 20px, weight 800.
- Body text: 16px.
- Labels: 14px, weight 600.

Text must fit within its container on mobile. Prefer wrapping over shrinking unless a compact control requires a single line.

## Shape And Spacing

- Core card radius: 14-18px.
- Small controls: 8px radius.
- Floating tab bar: 28px radius.
- Use generous vertical spacing on the home screen.
- Do not nest cards inside cards.
- Repeated list/card items may use shadows; page sections should not become floating cards.

## Icons

- Use `lucide-react-native` icons.
- Icon stroke width should generally be `2.2` to `2.5`.
- Use familiar symbols instead of text-only controls where possible:
  - Home
  - ReceiptText
  - ShoppingCart
  - CalendarDays
  - UserRound
  - Plus
  - Link2
  - PenLine
  - Camera
  - Clock3

## Feature State Rules

- Do not fake finished features.
- Missing product features should be marked as `#todo` in the UI where they appear.
- Current `#todo` features:
  - Scan recept
  - Profile preferences
  - Share sheet
  - RevenueCat subscriptions
  - Admin operations

## Recipe Cards

- Use the source post/page thumbnail when an imported recipe exposes one. Never assign random food photography to a real imported recipe.
- A manually entered recipe can use a user-uploaded image; until then it uses the neutral recipe-image state.
- Card content:
  - Image at top
  - Recipe title
  - Time with clock icon
  - Estimated price per person when available

## Shopping List

- Recipe detail never uses checkboxes for ingredients. It lets the user choose a shopping date, then add each ingredient with `Voeg toe`.
- Offer the next seven days as quick date choices and an `Andere datum` control for any later date.
- The Boodschappen tab shows upcoming lists first, followed by a month calendar with a visible marker on scheduled shopping days.
- Check-off items use real checkbox UI only inside a scheduled shopping-list detail screen, not `[ ]` or `[x]`.
- Stores and variants should be visible but secondary; never label this action `Vervang product`.
- Price and store/product details should be scan-friendly.

## Incomplete Imports

- Social imports first use public metadata/captions. Every Instagram Reel then also attempts a transcript; when the dedicated actor cannot resolve its audio, the cross-platform transcript actor is used automatically. A non-video Instagram post only uses the transcript fallback when its caption has no usable recipe text.
- Never silently invent missing quantities, servings, ingredients, or cut-off steps from a source.
- An incomplete import shows a clear `Recept incompleet` panel with its missing fields.
- AI completion is an explicit `AI-voorstel`; suggested fields stay visibly marked until the user accepts them.
- Incomplete recipes can be saved as concepts but cannot add ingredients to a shopping list.

## Recipe Library And Planning

- Recepten uses short filter chips generated from Dutch recipe tags; selected filters use the soft green active state.
- Every imported recipe shows its source platform icon and a Dutch import label.
- Planning is a week-by-week workspace with lunch and dinner slots. A slot can use a saved recipe or a manually typed meal and optional note.
- Planning controls use calendar navigation icons. The slot itself is the edit target; avoid separate explanatory controls.

## Implementation Rule

Before adding or restyling any screen, read this file and use `apps/mobile/lib/theme.ts` for shared colors, spacing, radii, typography, and shadows.
