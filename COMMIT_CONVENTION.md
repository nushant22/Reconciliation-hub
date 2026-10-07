# Commit Convention

This project follows the [Conventional Commits](https://www.conventionalcommits.org/) specification.

## Format

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

## Types

- **feat**: A new feature
- **fix**: A bug fix
- **docs**: Documentation only changes
- **style**: Changes that do not affect the meaning of the code (white-space, formatting, etc)
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **perf**: A code change that improves performance
- **test**: Adding missing tests or correcting existing tests
- **build**: Changes that affect the build system or external dependencies
- **ci**: Changes to CI configuration files and scripts
- **chore**: Other changes that don't modify src or test files
- **revert**: Reverts a previous commit

## Examples

### Feature
```
feat: add CSV export format option

Add support for exporting reconciliation results as CSV files.
This provides a lightweight alternative to Excel workbooks.
```

### Bug Fix
```
fix: correct amount parsing for negative values

Fixes issue where negative amounts in accounting format (500.00)
were not being parsed correctly.

Closes #123
```

### Documentation
```
docs: update installation instructions for Docker

Add Docker deployment section with docker-compose example.
```

### Breaking Change
```
feat!: change API response format

BREAKING CHANGE: The reconciliation API now returns results in a new format.
Clients will need to update their parsing logic.

Migration guide:
- Old: response.data.matches
- New: response.reconciliation.exact_matches
```

### Multiple Types
```
feat: add dashboard generation feature
docs: add DASHBOARD.md documentation
test: add dashboard generator tests
```

## Scopes (Optional)

You can add a scope to provide additional context:

```
feat(airlines): add refund state tracking
fix(matcher): handle duplicate keys correctly
docs(readme): update deployment section
test(sanitizer): add currency parsing tests
```

## Rules

1. **Type**: Must be one of the types listed above (lowercase)
2. **Scope**: Optional, must be lowercase
3. **Subject**: 
   - Required
   - Lowercase
   - No period at the end
   - Maximum 100 characters
4. **Body**: Optional, use to explain what and why (not how)
5. **Footer**: Optional, reference issues or breaking changes

## Tools

### Commitlint

This repository uses commitlint to enforce conventional commits on PRs.

Install locally:
```bash
npm install --save-dev @commitlint/{config-conventional,cli}
```

Validate a commit message:
```bash
echo "feat: add new feature" | npx commitlint
```

### Git Hook (Optional)

Add to `.git/hooks/commit-msg`:
```bash
#!/bin/sh
npx --no-install commitlint --edit $1
```

Make executable:
```bash
chmod +x .git/hooks/commit-msg
```

## Benefits

1. **Automated changelog generation**
2. **Semantic versioning automation**
3. **Clear commit history**
4. **Easy to understand changes**
5. **Better collaboration**

## Resources

- [Conventional Commits](https://www.conventionalcommits.org/)
- [Commitlint](https://commitlint.js.org/)
- [Semantic Versioning](https://semver.org/)
