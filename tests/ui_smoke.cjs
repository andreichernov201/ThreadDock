const { chromium } = require("playwright");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
    const root = path.resolve("archive/web");
    const types = {
        ".html": "text/html",
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
    };
    const server = http.createServer(async (req, res) => {
        const name = new URL(req.url, "http://localhost").pathname.replace(
            /^\/assets\//,
            "/",
        );
        const file = name === "/" ? "index.html" : name.slice(1);
        if (
            ![
                "index.html",
                "app.js",
                "style.css",
                "texts.js",
                "demo.js",
                "icon.svg",
            ].includes(file)
        ) {
            res.writeHead(404).end();
            return;
        }
        try {
            res.writeHead(200, { "Content-Type": types[path.extname(file)] });
            res.end(await fs.readFile(path.join(root, file)));
        } catch {
            res.writeHead(500).end();
        }
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    let browser;
    try {
        browser = await chromium.launch({
            channel: "chromium",
            args: ["--disable-gpu"],
        });
        const page = await browser.newPage({
            viewport: { width: 390, height: 844 },
        });
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("https://telegram.org/**", (route) => route.abort());
        const url = "http://127.0.0.1:" + server.address().port + "/?demo=1";
        async function screenshot(name) {
            const bytes = await page.screenshot();
            const directory = path.join(
                process.env.RUNNER_TEMP || ".deploy-work",
                "threaddock-ui",
            );
            await fs.mkdir(directory, { recursive: true });
            await fs.writeFile(path.join(directory, name + ".png"), bytes);
            if (process.env.UI_SCREENSHOT_LOG !== "1") return;
            const encoded = bytes.toString("hex").replace(
                /[0-9a-f]/g,
                (c) => String.fromCharCode(97 + parseInt(c, 16)),
            );
            for (let index = 0; index < encoded.length; index += 16000) {
                console.log(
                    "SCREENSHOT_CHUNK " +
                        JSON.stringify({
                            name,
                            data: encoded.slice(index, index + 16000),
                        }),
                );
            }
        }
        await page.goto(url);
        await page.locator(".event-row").first().waitFor();
        assert(await page.locator(".feed-day").count() > 0);
        assert.equal(await page.locator(".sidebar").isVisible(), false);
        assert(await page.locator(".bottom-nav").isVisible());
        await screenshot("mobile-feed");
        const navBounds = await page.locator(".bottom-nav").evaluate((el) => {
            const rect = el.getBoundingClientRect();
            return {
                top: rect.top,
                bottom: rect.bottom,
                height: innerHeight,
                position: getComputedStyle(el).position,
            };
        });
        console.log("UI_NAV_BOUNDS " + JSON.stringify(navBounds));
        assert(
            navBounds.top >= 0 && navBounds.top < navBounds.height - 40 &&
                navBounds.bottom <= navBounds.height + 1,
        );
        assert(
            await page.evaluate(() =>
                document.documentElement.scrollWidth <= innerWidth + 1
            ),
        );
        await page.locator('[data-action="filter"][data-mode="edited"]')
            .click();
        await page.locator(".event-row").first().click();
        await page.locator('dialog[open][data-view="record"]').waitFor();
        assert(await page.locator(".record-content").isVisible());
        assert(await page.locator(".version ins").count() > 0);
        assert(await page.locator(".version del").count() > 0);
        await screenshot("mobile-event");
        await page.locator('.record-toolbar [data-action="close-modal"]')
            .click();
        await page.locator('.bottom-nav [data-view="stats"]').click();
        await page.locator(".heatmap").waitFor();
        assert(await page.locator(".insight-metrics").isVisible());
        assert(await page.locator("#stats-chat").isVisible());
        assert.equal(await page.locator(".stat").count(), 4);
        await page.locator('.bottom-nav [data-view="settings"]').click();
        await page.locator('[data-action="delete-stats"]').click();
        await page.locator('dialog[open] #delete-form[data-kind="stats"]').waitFor();
        await page.locator('dialog[open] [data-action="close-modal"]').click();
        await page.locator('[data-action="delete-history"]').click();
        await page.locator('dialog[open] #delete-form[data-kind="history"]').waitFor();
        await page.locator('dialog[open] [data-action="close-modal"]').click();
        await page.locator('.bottom-nav [data-view="feed"]').click();
        await page.setViewportSize({ width: 1200, height: 900 });
        await page.locator(".event-row").first().waitFor();
        assert(
            await page.evaluate(() =>
                document.documentElement.scrollWidth <= innerWidth + 1
            ),
        );
        const sendPage = await browser.newPage({ viewport: { width: 390, height: 844 } });
        sendPage.on("pageerror", (error) => errors.push(error.message));
        await sendPage.route("https://telegram.org/**", (route) => route.abort());
        await sendPage.addInitScript(() => {
            window.Telegram = { WebApp: { initData: "fixture", ready() {}, expand() {}, onEvent() {} } };
        });
        const mediaId = "a".repeat(32);
        const media = {
            id: mediaId, kind: "photo", filename: "fixture.jpg", size: 123,
            chat_title: "Fixture", status: "ready", protected: 0, ephemeral: 1,
        };
        const confirmPath = `/api/media/${mediaId}/send/confirm`;
        const created = 1234567890.5;
        const responses = {
            "/api/auth": { token: "fixture", csrf: "fixture", locked: false, user: { id: 10 } },
            "/api/session": { owner: { enabled: 1, language: "ru" }, bot_username: "fixture_bot" },
            "/api/chats": { items: [] },
            "/api/events": { items: [], counts: {} },
            "/api/gallery": { items: [media] },
            [`/api/media/${mediaId}/send`]: { task_id: 14, status: "uncertain" },
            "/api/tasks/14": { id: 14, created_at: created, status: "uncertain" },
            [confirmPath]: { task_id: 15, status: "pending" },
            "/api/tasks/15": { id: 15, status: "done" },
        };
        let confirmations = 0;
        await sendPage.route("**/api/**", (route) => {
            const routePath = new URL(route.request().url()).pathname;
            if (routePath === confirmPath) confirmations++;
            return route.fulfill({ json: responses[routePath] || {} });
        });
        await sendPage.goto(url.split("?")[0]);
        await sendPage.locator('.bottom-nav [data-view="gallery"]').click();
        await sendPage.locator('[data-action="send-media"]').click();
        const confirmation = sendPage.locator('dialog[open] #send-retry-form');
        await confirmation.waitFor();
        await confirmation.locator('button[type="submit"]').click();
        assert.equal(confirmations, 0);
        await confirmation.locator('[name="checked_chat"]').check();
        const posted = sendPage.waitForRequest((req) => req.url().endsWith(confirmPath));
        await confirmation.locator('button[type="submit"]').click();
        assert.deepEqual((await posted).postDataJSON(), { task_id: 14, created_at: created, checked_chat: true });
        await confirmation.waitFor({ state: "hidden" });
        assert.equal(confirmations, 1);
        await sendPage.close();
        assert.deepEqual(errors, []);
        console.log(
            "UI_CHECK_PASSED mobile, desktop, event history, statistics and confirmed media retry",
        );
    } finally {
        try {
            await browser?.close();
        } finally {
            server.close();
        }
    }
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
