import asyncio
from playwright.async_api import async_playwright, expect

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width":1280,"height":800})
        await ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await ctx.new_page()

        await page.goto("https://demo.playwright.dev/todomvc/")
        box = page.get_by_placeholder("What needs to be done?")
        await box.fill("Write the Specreel spike")
        await box.press("Enter")
        await box.fill("Generate a watchable demo")
        await box.press("Enter")
        await box.fill("Ship it to the team")
        await box.press("Enter")

        await expect(page.get_by_test_id("todo-title")).to_have_count(3)

        # complete the first todo
        first = page.get_by_role("listitem").filter(has_text="Write the Specreel spike")
        await first.get_by_role("checkbox").check()
        await expect(first).to_have_class("completed")

        # filter to active
        await page.get_by_role("link", name="Active").click()
        await expect(page.get_by_test_id("todo-title")).to_have_count(2)

        # show count
        await expect(page.get_by_test_id("todo-count")).to_have_text("2 items left")

        await ctx.tracing.stop(path="todo-trace.zip")
        await browser.close()
        print("trace written")

asyncio.run(main())
