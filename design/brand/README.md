# Airkeep brand assets

The filament-loop mark represents a spool and its loose filament end. The wordmark uses DM Serif Display, converted to outlines so the SVGs don't depend on an installed font. Its SIL Open Font License is in [fonts/OFL.txt](fonts/OFL.txt).

## Files

| Asset | Location | Use |
| --- | --- | --- |
| Logo mark | `logo/airkeep-mark-*.svg` | Charcoal, ivory, amber, black and white versions with transparent backgrounds |
| Logo and wordmark | `logo/airkeep-lockup-*.svg` | `light` for light surfaces; `dark` for dark surfaces |
| README banner | [github/readme-banner.png](github/readme-banner.png) | 1280 × 320; used in the repository README |
| GitHub social preview | [github/social-preview.png](github/social-preview.png) | 1280 × 640; upload under repository Settings → General → Social preview |
| Avatar | [github/avatar.png](github/avatar.png) | 1024 × 1024; keeps the mark inside a circular crop |
| Native Apple icon | [../../apple/AppIcon.icon](../../apple/AppIcon.icon) | Shared Icon Composer document used by both app targets |
| Mark source | [source/filament-loop.svg](source/filament-loop.svg) | Editable silhouette used for the logo variants |

The GitHub artwork includes editable SVG versions alongside the PNGs. Logo variants are kept as vectors; export a PNG from an SVG editor when needed.

## Apple icon

Open `apple/AppIcon.icon` in Icon Composer to edit the app icon. Both apps reference it through `ASSETCATALOG_COMPILER_APPICON_NAME`. Xcode generates the required sizes and platform renditions when building the apps, including compatibility images for the minimum deployment target. Separate PNG and ICNS exports aren't needed for builds.

The light appearance has a charcoal mark on ivory. Dark uses amber on charcoal. The foreground has no corner mask or baked-in lighting. Icon Composer applies the mask, material, lighting and the user's tint at render time.

## Palette

| Color | Hex |
| --- | --- |
| Charcoal | `#0E0F0F` |
| Ivory | `#F5F0E6` |
| Amber | `#FFB82E` |

Keep the mark's proportions and the gap between the loop and its tail. Leave at least one quarter of the loop's diameter clear around standalone uses. At small sizes, use a single color and keep the central opening visible.
