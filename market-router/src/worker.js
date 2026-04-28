export default {
  async fetch(request) {
    try {
      const url = new URL(request.url);
      const country = request.cf?.country || "";
      const cookies = parseCookies(request.headers.get("cookie") || "");

      // Manual override via query ?market=us|eu|fr
      const marketParam = url.searchParams.get("market");
      const manualMarket =
        marketParam === "us" || marketParam === "eu" || marketParam === "fr" ? marketParam : null;

      // Priority: manual override > cookie > geo
      const inferredMarket = country === "US" ? "us" : "eu";
      const market = manualMarket || cookies.market || inferredMarket;

      const isUsPath = url.pathname === "/us" || url.pathname.startsWith("/us/");
      const isFrPath = url.pathname === "/fr" || url.pathname.startsWith("/fr/");
      const isAsset =
        url.pathname.startsWith("/images/") ||
        url.pathname.startsWith("/styles/") ||
        url.pathname.startsWith("/scripts/") ||
        url.pathname.startsWith("/vendor/") ||
        /\.[a-z0-9]+$/i.test(url.pathname);

      let targetPath = null;

      // Redirect only HTML pages, not assets
      if (!isAsset) {
        if (market === "us" && !isUsPath) {
          targetPath = url.pathname === "/" ? "/us/" : `/us${url.pathname}`;
        }
        if (market === "eu" && (isUsPath || isFrPath)) {
          targetPath = url.pathname.replace(/^\/(us|fr)(?=\/|$)/, "") || "/";
        }
        if (market === "fr" && !isFrPath) {
          targetPath = url.pathname === "/" ? "/fr/" : `/fr${url.pathname}`;
        }
      }

      if (targetPath) {
        const target = new URL(request.url);
        target.pathname = targetPath;
        // Keep URL bar clean once manual market is consumed.
        target.searchParams.delete("market");
        const res = new Response(null, {
          status: 307,
          headers: {
            Location: target.toString(),
          },
        });

        if (manualMarket) {
          res.headers.append(
            "Set-Cookie",
            `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
          );
        }
        return res;
      }

      // If manual market is provided but path is already correct, clean the query param.
      if (manualMarket && request.method === "GET") {
        const clean = new URL(request.url);
        clean.searchParams.delete("market");
        if (clean.toString() !== request.url) {
          const res = new Response(null, {
            status: 307,
            headers: {
              Location: clean.toString(),
            },
          });
          res.headers.append(
            "Set-Cookie",
            `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
          );
          return res;
        }
      }

      const originRes = await fetch(request);

      // Persist manual market choice even when no redirect happens
      if (!manualMarket) return originRes;
      const res = new Response(originRes.body, originRes);
      res.headers.append(
        "Set-Cookie",
        `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
      );
      return res;
    } catch (_err) {
      // Fail open so site traffic is never blocked by router errors.
      return fetch(request);
    }
  },
};

function parseCookies(raw) {
  const out = {};
  for (const part of raw.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (!k) continue;
    const value = v.join("=") || "";
    try {
      out[k] = decodeURIComponent(value);
    } catch {
      out[k] = value;
    }
  }
  return out;
}
