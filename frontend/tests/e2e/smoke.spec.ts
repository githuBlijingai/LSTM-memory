import { expect, test } from "@playwright/test";

test("根路径重定向到 M0 概览骨架", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();
  await expect(page.getByText("AG-05 待实现")).toBeVisible();
  await expect(page.getByText("业务功能待实现")).toBeVisible();
});
