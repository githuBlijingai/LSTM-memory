import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import { isForbidden, isInterfaceUnavailable, isUnauthorized } from "@/lib/api";
import { formatDateTime, formatScore, utcTooltip } from "@/lib/format";

const error = (status: number) =>
  new ApiError("x", { status, code: "TEST", requestId: "req" });

describe("API 错误分类", () => {
  it("区分鉴权、权限与接口未就绪", () => {
    expect(isUnauthorized(error(401))).toBe(true);
    expect(isForbidden(error(403))).toBe(true);
    expect(isInterfaceUnavailable(error(404))).toBe(true);
    expect(isInterfaceUnavailable(error(503))).toBe(true);
    expect(isInterfaceUnavailable(error(500))).toBe(false);
    expect(isInterfaceUnavailable(new Error("boom"))).toBe(false);
  });
});

describe("时间与分数格式化", () => {
  it("空值使用占位符", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatScore(undefined)).toBe("—");
    expect(utcTooltip("bad-date")).toBe("");
  });

  it("输出稳定的 UTC 提示与定点分数", () => {
    expect(utcTooltip("2026-11-12T03:20:00Z")).toBe("2026-11-12T03:20:00.000Z");
    expect(formatScore(0.81234)).toBe("0.8123");
    expect(formatScore(0.82, 2)).toBe("0.82");
  });
});
