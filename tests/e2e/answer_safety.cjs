// Question wording alone is not disclosure; a concrete account-like number is.
const containsAccountNumber = (text) => {
  const accountLabel = '(?:银行卡|银行账户(?:号码)?|账户(?:号码)?|卡号|账号)';
  const numberToken = '\\d(?:[\\d\\s-]{8,}\\d)';
  return new RegExp(`${accountLabel}[^\\d]{0,24}${numberToken}`, 'u').test(text)
    || new RegExp(`${numberToken}[^\\d]{0,24}${accountLabel}`, 'u').test(text)
    || new RegExp(numberToken, 'u').test(text);
};

module.exports = { containsAccountNumber };
