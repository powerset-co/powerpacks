/* The connections count at the top of LinkedIn's connections page.
 *
 * The page is in the account's language ("1,234 connections", "1.234 contactos",
 * "1.234 Kontakte", "つながり1,234人"), so the line is known by its shape, not its
 * word: a short line holding one number next to some letters. Names and dates hold
 * more than one number or no letters next to one.
 *
 * Created: 2026-10-28
 */
function shownCount(texts) {
  for (const text of texts) {
    if (text.length > 40 || !/\p{L}/u.test(text)) continue;
    const match = text.match(/^\D*(\d[\d.,\s]*)\D*$/u);
    if (match) return Number(match[1].replace(/\D/g, ""));
  }
  return null;
}

module.exports = { shownCount };
