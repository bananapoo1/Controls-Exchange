# Controls Exchange pre-launch checklist

Use this as the working list between staging and the first real outreach campaign.

## 1. Identity and trust

- [x] Approved logo/mark and brand palette implemented.
- [x] Brand rules documented in `BRAND.md`.
- [ ] Choose and register the production domain.
- [ ] Run proper UK name/trademark clearance before meaningful marketing spend.
- [ ] Add the actual legal entity name, registered address and company number to legal/footer copy.
- [ ] Create branded addresses such as `hello@`, `support@` and `privacy@` on the production domain.

## 2. Email

- [ ] Configure Resend or SMTP with the production sending domain.
- [ ] Set SPF, DKIM and DMARC.
- [ ] Test verification, password reset, saved-search alerts, wanted alerts and order notifications end to end.
- [ ] Check HTML/plain-text rendering in Gmail, Outlook and iPhone Mail.
- [ ] Make sure every marketing email has a clear opt-out and suppression workflow.

## 3. Product content

- [ ] Review Privacy and Terms with the real legal entity details.
- [ ] Add a visible support/contact route before broad launch.
- [ ] Add branded 404/500/help states before broad launch.
- [ ] Decide whether a public pricing page is useful before the first cold-outreach campaign.
- [ ] Prepare a short FAQ for suppliers: inventory privacy, verification, 90-day trial, commission, RFQs and cancellation.

## 4. Marketplace liquidity

- [ ] Recruit the first 10 founding suppliers before broad buyer promotion.
- [ ] Ask each supplier for obsolete/surplus/dead-stock exports first rather than their entire ERP catalogue.
- [ ] Manually clean and import the first supplier inventories if that removes onboarding friction.
- [ ] Ensure the homepage does not display invented network/supplier counts.
- [ ] Seed enough real catalogue identities to make common Trend/Honeywell/Schneider/Siemens/JCI searches useful.

## 5. Measurement

Track the smallest useful funnel first:

### Buyer
`landing -> search -> register -> email verified -> RFQ or wanted request`

### Supplier
`supplier page -> register -> email verified -> inventory uploaded -> company approved -> first RFQ`

- [ ] Pick an analytics provider or implement first-party event reporting.
- [ ] Keep analytics optional/configurable and avoid unnecessary personal-data capture.
- [ ] Record acquisition source for founding supplier outreach where practical.

## 6. Live infrastructure

- [ ] Complete the temporary Render staging deployment in `STAGING_DEPLOY.md`.
- [ ] Test the staging URL on desktop and a real iPhone.
- [ ] Complete the live checks in `SETUP_AND_TESTING.md`.
- [ ] Choose durable production hosting before accepting irreplaceable order documents.
- [ ] Use production PostgreSQL with backups and a tested restore procedure.
- [ ] Run web, feed worker and webhook worker as separate production services/processes.
- [ ] Configure persistent/private order-document storage plus malware scanning before unknown companies upload files.
- [ ] Configure Sentry or equivalent monitoring/alerting.

## 7. Payments

- [ ] Create real Stripe products/prices in test mode first.
- [ ] Test no-card founding trial -> payment method -> paid conversion.
- [ ] Test no payment method at trial end.
- [ ] Test failed renewal, cancellation and Billing Portal changes.
- [ ] Decide VAT/tax treatment and invoice wording before live billing.
- [ ] Keep buyers free at launch unless the commercial strategy changes deliberately.

## 8. Security / abuse

- [ ] Use unique production secrets for `SECRET_KEY` and `INTEGRATION_ENCRYPTION_KEY`.
- [ ] Restrict `ALLOWED_HOSTS` to the live hostname/domain.
- [ ] Verify secure cookies and HTTPS/HSTS on production.
- [ ] Review upload MIME/type validation with real sample files.
- [ ] Add malware scanning for commercial document uploads.
- [ ] Test webhook SSRF controls against public/private targets.
- [ ] Test API-key revocation/rate limits under realistic concurrency.
- [ ] Remove all development/demo admin credentials and demo data from production.

## 9. Outreach readiness

- [x] Initial UK launch prospect workbook created.
- [ ] Verify the first 10–20 contact records immediately before outreach.
- [ ] Screen telephone numbers against TPS/CTPS where required.
- [ ] Prepare the founding-supplier email and call script.
- [ ] Prepare a simple one-page PDF/landing link explaining the supplier proposition.
- [ ] Decide how inventory files from early suppliers will be received securely.
- [ ] Track objections and rewrite outreach after the first 10 conversations rather than mass-mailing the whole list.

## 10. Launch decision

Do not wait for perfection. A sensible private-beta threshold is:

- 10+ verified suppliers;
- useful real inventory across several common BMS brands;
- buyer registration/RFQ flow tested live;
- supplier upload + approval flow tested live;
- working transactional email;
- monitoring enabled;
- basic legal/company details present;
- no known critical security or data-loss issue.

At that point the priority should shift from feature-building to real usage, supplier conversations and fixing what actual customers struggle with.
