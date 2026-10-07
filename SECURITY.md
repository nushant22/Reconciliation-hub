# Security Policy

## Supported Versions

We currently support security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| main    | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly:

### DO NOT

- Open a public GitHub issue
- Discuss the vulnerability publicly

### DO

1. Email the maintainers at [your-email@example.com]
2. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if available)

### Response Timeline

- **Initial Response:** Within 48 hours
- **Status Update:** Within 7 days
- **Fix Timeline:** Depends on severity (critical issues prioritized)

## Security Best Practices

### For Users

1. **Never commit secrets**
   - Keep `.streamlit/secrets.toml` out of version control
   - Use environment variables for sensitive data
   - Review `.gitignore` before committing

2. **Validate uploaded files**
   - Only process files from trusted sources
   - Be aware of potential malicious content in Excel/CSV files

3. **Keep dependencies updated**
   ```bash
   pip install --upgrade -r requirements.txt
   ```

4. **Use HTTPS in production**
   - Deploy behind a reverse proxy (nginx, Apache)
   - Enable SSL/TLS certificates

5. **Limit database access**
   - Use read-only database credentials where possible
   - Restrict database network access

### For Developers

1. **Code review requirements**
   - All PRs must be reviewed
   - Security-sensitive changes require maintainer approval

2. **Dependency management**
   - Pin dependency versions in requirements.txt
   - Review security advisories for dependencies
   - Use tools like `pip-audit` or `safety`

3. **Input validation**
   - Sanitize all user inputs
   - Validate file formats and sizes
   - Handle errors gracefully

4. **Secrets management**
   - Never hardcode credentials
   - Use secrets.toml or environment variables
   - Document required secrets in secrets.toml.example

## Known Security Considerations

### File Upload Risks

- The application processes user-uploaded Excel and CSV files
- Malicious files could potentially exploit parsing libraries
- **Mitigation:** Keep dependencies updated, validate file sizes

### Database Injection

- SQL queries use parameterized statements
- **Current status:** Protected against SQL injection

### Authentication

- The current version does not include built-in authentication
- **Recommendation:** Deploy behind an authentication proxy in production
- Consider adding authentication for production use cases

### Data Privacy

- Uploaded files and reconciliation outputs may contain sensitive financial data
- **Recommendation:** 
  - Use encrypted storage
  - Implement access controls
  - Clear temporary files after processing
  - Consider data retention policies

## Security Updates

Security patches will be released as soon as possible after verification. Users are strongly encouraged to:

- Watch this repository for security announcements
- Subscribe to release notifications
- Keep deployments updated

## Attribution

We appreciate responsible disclosure and will acknowledge researchers who report vulnerabilities (with permission).
