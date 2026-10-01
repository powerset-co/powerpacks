import { describe, expect, it } from "vitest"

import { LINKEDIN_URL_RE, routeGuidance } from "./guidance"

const fix = (url: string) => ({ kind: "fix", url })
const retarget = (guidance: string) => ({ kind: "retarget", guidance })

describe("routeGuidance", () => {
  it.each([
    ["https://www.linkedin.com/in/jordan-bravo", "https://www.linkedin.com/in/jordan-bravo"],
    ["http://www.linkedin.com/in/jordan-bravo", "http://www.linkedin.com/in/jordan-bravo"],
    ["https://linkedin.com/in/jordan-bravo", "https://linkedin.com/in/jordan-bravo"],
    ["www.linkedin.com/in/jordan-bravo", "www.linkedin.com/in/jordan-bravo"],
    ["linkedin.com/in/jordan-bravo", "linkedin.com/in/jordan-bravo"],
    ["https://uk.linkedin.com/in/jordan-bravo", "https://uk.linkedin.com/in/jordan-bravo"],
    ["HTTPS://WWW.LINKEDIN.COM/IN/Jordan-Bravo", "HTTPS://WWW.LINKEDIN.COM/IN/Jordan-Bravo"],
    ["https://www.linkedin.com/in/jordan.bravo_99", "https://www.linkedin.com/in/jordan.bravo_99"],
  ])("sends the profile URL %s to the free decide", (text, url) => {
    expect(routeGuidance(text)).toEqual(fix(url))
  })

  it("keeps the URL alone: no trailing slash, query or fragment", () => {
    expect(routeGuidance("https://www.linkedin.com/in/jordan-bravo/")).toEqual(
      fix("https://www.linkedin.com/in/jordan-bravo"),
    )
    expect(routeGuidance("https://www.linkedin.com/in/jordan-bravo?utm_source=share#about")).toEqual(
      fix("https://www.linkedin.com/in/jordan-bravo"),
    )
  })

  it("trims the box before routing", () => {
    expect(routeGuidance("  \n linkedin.com/in/jordan-bravo \t")).toEqual(fix("linkedin.com/in/jordan-bravo"))
    expect(routeGuidance("  the one at Acme \n")).toEqual(retarget("the one at Acme"))
  })

  it("sends text that contains a profile URL to the FREE route, with the URL alone", () => {
    expect(routeGuidance("wrong Jordan, it is https://www.linkedin.com/in/jordan-bravo-2 at Acme")).toEqual(
      fix("https://www.linkedin.com/in/jordan-bravo-2"),
    )
  })

  it("takes the first profile URL when several are typed", () => {
    expect(routeGuidance("linkedin.com/in/jordan-bravo or linkedin.com/in/casey-delta")).toEqual(
      fix("linkedin.com/in/jordan-bravo"),
    )
  })

  it.each([
    "the founder of Example Labs, not the dentist",
    "Jordan Bravo, Springfield",
    "https://www.linkedin.com/company/example-labs",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com/pub/jordan-bravo",
    "https://example.com/in/jordan-bravo",
    "linkedin jordan-bravo",
    "casey@example.com",
  ])("sends %s to the paid re-research", (text) => {
    expect(routeGuidance(text)).toEqual(retarget(text))
  })

  it.each(["", " ", "\n\t  "])("sends nothing for an empty box (%j)", (text) => {
    expect(routeGuidance(text)).toEqual({ kind: "nothing" })
  })

  it("is the old page's pattern, unchanged", () => {
    expect(LINKEDIN_URL_RE.source).toBe(
      String.raw`(?:https?:\/\/)?(?:[a-z]+\.)?linkedin\.com\/in\/[A-Za-z0-9._-]+`,
    )
    expect(LINKEDIN_URL_RE.flags).toBe("i")
  })
})
