// Cloudflare Pages Function — proxies every /api/* request to the Render
// backend. Replaces the `_redirects` /api/* rule, which silently refused
// POST requests (CF Pages _redirects only proxies GET).
//
// Why this file exists instead of a hardcoded URL in the browser JS:
// the Render origin lives in exactly one place (here), so a future move
// (e.g. api.howtocookathome.com) changes one line in one file, and the
// browser keeps hitting same-origin /api/* — no CORS, no client edits.

const ORIGIN = "https://howtocookathome.onrender.com";

export async function onRequest(context) {
    const url = new URL(context.request.url);
    const target = ORIGIN + url.pathname + url.search;

    // Forward the original request verbatim: method, headers, body.
    // Only Host/CF-* headers are rewritten by fetch() automatically.
    const upstream = await fetch(target, context.request);

    // Clone the response so headers are mutable (Cloudflare returns
    // immutable Response objects from fetch()).
    const response = new Response(upstream.body, upstream);
    response.headers.set("Access-Control-Allow-Origin", "*");
    return response;
}
