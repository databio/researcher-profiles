/**
 * Copy-pasteable remedies for common conformance failures, keyed by check
 * id and detected hosting platform.
 */

type HostKind = "s3" | "r2" | "cloudflare-pages" | "github-pages" | "nginx" | "apache" | "generic";

function detectHost(url: string): HostKind {
  try {
    const hostname = new URL(url).hostname;
    if (hostname.endsWith(".s3.amazonaws.com") || hostname.includes(".s3.") || hostname.includes(".s3-")) return "s3";
    if (hostname.endsWith(".r2.dev")) return "r2";
    if (hostname.endsWith(".pages.dev")) return "cloudflare-pages";
    if (hostname.endsWith(".github.io")) return "github-pages";
  } catch {
    // fall through
  }
  return "generic";
}

const CORS_FIXES: Record<HostKind, string> = {
  s3: `AWS S3 CORS configuration:

[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedOrigins": ["*"],
    "ExposeHeaders": [],
    "MaxAgeSeconds": 86400
  }
]

Apply with: aws s3api put-bucket-cors --bucket YOUR_BUCKET --cors-configuration file://cors.json`,

  r2: `Cloudflare R2 CORS policy (set in the R2 dashboard or via API):

[
  {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 86400
  }
]`,

  "cloudflare-pages": `Add a _headers file to your Pages project root:

/*
  Access-Control-Allow-Origin: *`,

  "github-pages": `GitHub Pages already sends Access-Control-Allow-Origin: *. If you're
seeing a CORS error, it may be a different issue (wrong URL, mixed content).`,

  nginx: `Add to your nginx server or location block:

add_header Access-Control-Allow-Origin "*" always;`,

  apache: `Add to your Apache configuration or .htaccess:

Header set Access-Control-Allow-Origin "*"`,

  generic: `Ask your server administrator to add this HTTP response header:

Access-Control-Allow-Origin: *

This header must be present on every response, including for .jsonld, .json,
and .bin files. Without it, browsers cannot read the response body.`,
};

const CONTENT_TYPE_FIXES: Record<HostKind, string> = {
  s3: `Set the Content-Type when uploading .jsonld files:

aws s3 cp profile.jsonld s3://BUCKET/PATH/profile.jsonld \\
  --content-type "application/ld+json"`,

  r2: `Set Content-Type in the upload metadata. R2 inherits the Content-Type
from the upload: set it to "application/ld+json" when uploading .jsonld files.`,

  "cloudflare-pages": `Add to your _headers file:

/*.jsonld
  Content-Type: application/ld+json`,

  "github-pages": `GitHub Pages serves .jsonld as application/octet-stream. Consider also
publishing the manifest as a .json file and linking to it.`,

  nginx: `Add to your nginx configuration:

types {
  application/ld+json jsonld;
}`,

  apache: `Add to your Apache configuration or .htaccess:

AddType application/ld+json .jsonld`,

  generic: `Configure your server to serve .jsonld files with:

Content-Type: application/ld+json`,
};

/**
 * Get a copy-pasteable fix for a check failure.
 */
export function getFix(checkId: string, url: string): string | undefined {
  const host = detectHost(url);

  switch (checkId) {
    case "cors":
      return CORS_FIXES[host] || CORS_FIXES.generic;
    case "content-type-jsonld":
      return CONTENT_TYPE_FIXES[host] || CONTENT_TYPE_FIXES.generic;
    default:
      return undefined;
  }
}
