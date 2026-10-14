// Question wording alone is not disclosure; a concrete account-like number is.
const containsAccountNumber = (text, sourceTitles = []) => {
  // Only remove exact titles already recorded by this import run. Arbitrary
  // quoted text must still be checked (it can contain an invented number).
  for (const title of sourceTitles) {
    if (title) text = text.split(title).join('');
  }
  return /\d(?:[\d\s-]{8,}\d)/u.test(text);
};

module.exports = { containsAccountNumber };
