import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";


const [
  pageUrl,
  widthValue,
  heightValue,
  screenshotPath,
  selector = "",
] = process.argv.slice(2);

if (!pageUrl || !widthValue || !heightValue || !screenshotPath) {
  console.error(
    "Usage: node tools/render_landing.mjs URL WIDTH HEIGHT SCREENSHOT [SELECTOR]",
  );
  process.exit(2);
}

const width = Number.parseInt(widthValue, 10);
const height = Number.parseInt(heightValue, 10);
if (!Number.isInteger(width) || !Number.isInteger(height)) {
  throw new Error("Viewport dimensions must be integers.");
}

const chromePath =
  process.env.CHROME_PATH
  ?? path.join(
    process.env.LOCALAPPDATA ?? "",
    "Google",
    "Chrome",
    "Application",
    "chrome.exe",
  );
const port = 12000 + Math.floor(Math.random() * 20000);
const profile = path.join(
  os.tmpdir(),
  `rechka-landing-${process.pid}-${Date.now()}`,
);
await mkdir(path.dirname(screenshotPath), { recursive: true });

const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  {
    windowsHide: true,
    stdio: "ignore",
  },
);

async function waitForTarget() {
  let lastError;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const target = targets.find((item) => item.type === "page");
      if (target?.webSocketDebuggerUrl) {
        return target.webSocketDebuggerUrl;
      }
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw lastError ?? new Error("Chrome DevTools target did not start.");
}

const websocketUrl = await waitForTarget();
const socket = new WebSocket(websocketUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let requestId = 0;
const pending = new Map();
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (!message.id || !pending.has(message.id)) {
    return;
  }
  const { resolve, reject } = pending.get(message.id);
  pending.delete(message.id);
  if (message.error) {
    reject(new Error(message.error.message));
  } else {
    resolve(message.result);
  }
});

function command(method, params = {}) {
  requestId += 1;
  const id = requestId;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
  });
}

try {
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await command("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  await command("Page.navigate", { url: pageUrl });
  await new Promise((resolve) => setTimeout(resolve, 500));

  if (selector) {
    await command("Runtime.evaluate", {
      expression: `
        document.querySelector(${JSON.stringify(selector)})
          ?.scrollIntoView({ block: "start", behavior: "instant" })
      `,
      returnByValue: true,
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
  }

  const evaluation = await command("Runtime.evaluate", {
    expression: `
      (() => {
        const visibleOverflow = [...document.querySelectorAll("body *")]
          .map((element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
              tag: element.tagName,
              id: element.id,
              className:
                typeof element.className === "string"
                  ? element.className
                  : "",
              left: Math.round(rect.left),
              right: Math.round(rect.right),
              width: Math.round(rect.width),
              display: style.display,
              visibility: style.visibility,
            };
          })
          .filter(
            (item) =>
              item.display !== "none"
              && item.visibility !== "hidden"
              && (item.left < -1 || item.right > innerWidth + 1),
          );
        const heading = document.querySelector("h1")?.getBoundingClientRect();
        return {
          innerWidth,
          innerHeight,
          scrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth,
          heading: heading
            ? {
                left: Math.round(heading.left),
                right: Math.round(heading.right),
                width: Math.round(heading.width),
              }
            : null,
          visibleOverflow: visibleOverflow.slice(0, 30),
        };
      })()
    `,
    returnByValue: true,
  });
  const screenshot = await command("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  console.log(JSON.stringify(evaluation.result.value, null, 2));
} finally {
  socket.close();
  chrome.kill();
}
