# React + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend using TypeScript with type-aware lint rules enabled. Check out the [TS template](https://github.com/vitejs/vite/tree/main/packages/create-vite/template-react-ts) for information on how to integrate TypeScript and Oxlint's TypeScript related rules in your project.

## Environment

| Variable | Required | Used for |
|---|---|---|
| `VITE_GOOGLE_MAPS_KEY` | optional | Google Places search in the search bar, and the Street View thumbnail in the building detail panel |

Set it locally in `map/.env.local`, and for deploys with:

```
vercel env add VITE_GOOGLE_MAPS_KEY
```

The key is inlined into the client bundle by Vite, so it is public by design. Protect it
in the Google Cloud console with an **HTTP referrer** restriction (the Vercel domains only)
and an **API** restriction limited to *Places API (New)* and *Street View Static API*.

Both features degrade quietly when the variable is unset: Places search is skipped and only
NYC GeoSearch results show, and the Street View thumbnail is not rendered.
