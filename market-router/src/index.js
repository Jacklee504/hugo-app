export default {
  async fetch(request) {
    const url = new URL(request.url);
    const country = request.cf?.country || "";
    const cookies = parseCookies(request.headers.get("cookie") || "");

    // Manual override: ?market=us or ?market=ie or ?market=fr
    const marketParam = url.searchParams.get("market");
    const normalizedMarketParam = marketParam === "eu" ? "ie" : marketParam;
    const manualMarket =
      normalizedMarketParam === "us" || normalizedMarketParam === "ie" || normalizedMarketParam === "fr"
        ? normalizedMarketParam
        : null;

    // Priority: manual > cookie > geo
    const inferredMarket = country === "US" ? "us" : "ie";
    const cookieMarket = cookies.market === "eu" ? "ie" : cookies.market;
    const market = manualMarket || cookieMarket || inferredMarket;

    const isUsPath = url.pathname === "/us" || url.pathname.startsWith("/us/");
    const isFrPath = url.pathname === "/fr" || url.pathname.startsWith("/fr/");
    const isAsset =
      url.pathname.startsWith("/images/") ||
      url.pathname.startsWith("/styles/") ||
      url.pathname.startsWith("/scripts/") ||
      url.pathname.startsWith("/vendor/") ||
      /\.(css|js|mjs|png|jpg|jpeg|gif|svg|webp|ico|woff|woff2|txt|xml|json|map)$/i.test(
        url.pathname
      );

    let targetPath = null;

    if (!isAsset) {
      if (market === "us" && !isUsPath) {
        targetPath = url.pathname === "/" ? "/us/" : `/us${url.pathname}`;
      } else if (market === "ie" && (isUsPath || isFrPath)) {
        targetPath = url.pathname.replace(/^\/(us|fr)(?=\/|$)/, "") || "/";
      } else if (market === "fr" && !isFrPath) {
        targetPath = url.pathname === "/" ? "/fr/" : `/fr${url.pathname}`;
      }
    }

    if (targetPath) {
      const target = new URL(request.url);
      target.pathname = targetPath;
      target.searchParams.delete("market");
      const res = Response.redirect(target.toString(), 307);

      if (manualMarket) {
        res.headers.append(
          "Set-Cookie",
          `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
        );
      }
      return res;
    }

    if (manualMarket && request.method === "GET") {
      const clean = new URL(request.url);
      clean.searchParams.delete("market");
      if (clean.toString() !== request.url) {
        const res = Response.redirect(clean.toString(), 307);
        res.headers.append(
          "Set-Cookie",
          `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
        );
        return res;
      }
    }

    const originRes = await fetch(request);

    if (!manualMarket) return originRes;

    // Persist manual market choice even without redirect
    const res = new Response(originRes.body, originRes);
    res.headers.append(
      "Set-Cookie",
      `market=${manualMarket}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`
    );
    return res;
  },
};

function parseCookies(raw) {
  const out = {};
  for (const part of raw.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (!k) continue;
    out[k] = decodeURIComponent(v.join("=") || "");
  }
  return out;
}
