#!/usr/bin/env node
/**
 * @file Lints the package-lock.json file to ensure it is in sync with package.json.
 *
 * Usage:
 *   lint-lockfile [options] [directory]
 *
 * Options:
 *   --warn    Report issues as warnings instead of failing. The lockfile is
 *             still regenerated on disk, but the process exits 0.
 *
 * Exit codes:
 *   0  Lockfile is in sync (or --warn was passed)
 *   1  Unexpected error
 *   2  Lockfile drift detected
 */

import * as assert from "node:assert/strict";
import { findPackageJSON } from "node:module";
import { dirname } from "node:path";
import { isDeepStrictEqual, parseArgs, stripVTControlCharacters } from "node:util";

import { parseCWD, reportAndExit } from "./utils/commands.mjs";
import { corepack } from "./utils/corepack.mjs";
import { gitStatus } from "./utils/git.mjs";
import { createLogger } from "./utils/logging.mjs";
import { findNPMPackage, loadJSON, npm } from "./utils/node.mjs";

//#region Utilities

const parsedArgs = parseArgs({
    options: {
        "warn": {
            type: "boolean",
            default: false,
            description: "Report issues as warnings instead of failing",
        },
        "skip-git": {
            type: "boolean",
            default: !!process.env.CI,
            description:
                "Skip checking for uncommitted changes (use with --warn to ignore drift without reporting)",
        },
    },
    allowPositionals: true,
});

const logger = createLogger("lint:lockfile");

const { values: options, positionals } = parsedArgs;
const cwd = parseCWD(positionals);

/**
 * Exit code when lockfile drift is detected (distinct from general errors)
 */
const EXIT_DRIFT = 2;

/**
 * @returns {Promise<string[]>} The list of issues detected.
 */
async function run() {
    /** @type {string[]} */
    const issues = [];

    /**
     * Records an issue. In strict mode, throws immediately.
     * In warn mode, collects the message for later reporting.
     *
     * @param {boolean} ok
     * @param {string} message
     */
    const check = (ok, message) => {
        if (ok) return;

        if (options.warn) {
            issues.push(message);
            return;
        }

        assert.fail(message);
    };

    /**
     * Checks deep equality of two values. In strict mode, throws if they are not equal.
     * In warn mode, records an issue instead.
     *
     * @param {unknown} actual
     * @param {unknown} expected
     * @param {string} message
     */
    const checkDeep = (actual, expected, message) => {
        if (options.warn) {
            if (!isDeepStrictEqual(actual, expected)) {
                issues.push(message);
            }

            return;
        }

        assert.deepStrictEqual(actual, expected, message);
    };

    logger.info(`Linting lockfile integrity in: ${cwd}`);

    // MARK: Locate files

    const resolvedPath = import.meta.resolve(cwd);
    const packageJSONPath = findPackageJSON(resolvedPath);

    assert.ok(
        packageJSONPath,
        "Could not find package.json in the current directory or any parent directories",
    );

    const packageDir = dirname(packageJSONPath);
    const { packageLockPath } = await findNPMPackage(packageDir);
    const lockfileDir = dirname(packageLockPath);
    const isWorkspace = lockfileDir !== packageDir;

    const corepackVersion = await corepack`--version`().catch(() => null);
    const useCorepack = !!corepackVersion;
    logger.info("corepack", corepackVersion || "disabled");

    const expected = {
        lockfile: await loadJSON(packageLockPath),
        package: await loadJSON(packageJSONPath),
    };

    logger.info(`package.json: ${packageJSONPath} (${expected.package.name})`);
    logger.info(`package-lock.json: ${packageLockPath}${isWorkspace ? " (workspace root)" : ""}`);

    // MARK: Uncommitted changes

    if (options["skip-git"]) {
        logger.warn("Skipping git status check");
    } else {
        const packageStatus = await gitStatus(packageJSONPath);
        const lockfileStatus = await gitStatus(packageLockPath);

        if (!packageStatus.available || !lockfileStatus.available) {
            logger.warn("Git is not available; skipping uncommitted change detection.");
        } else {
            check(packageStatus.clean, `package.json has uncommitted changes: ${packageJSONPath}`);

            check(
                lockfileStatus.clean,
                `package-lock.json has uncommitted changes: ${packageLockPath}`,
            );
        }
    }

    // MARK: Regenerate

    const npmVersion = await npm`--version`({ useCorepack });

    logger.info(`Detected npm version: ${npmVersion}`);

    await npm`install --package-lock-only`({
        cwd: lockfileDir,
        useCorepack,
    });

    logger.info("npm install complete.");

    const actual = {
        lockfile: await loadJSON(packageLockPath),
        package: await loadJSON(packageJSONPath),
    };

    // MARK: Compare

    assert.deepStrictEqual(
        actual.package,
        expected.package,
        `package.json was unexpectedly modified during lockfile check: ${packageJSONPath}`,
    );

    try {
        checkDeep(
            actual.lockfile,
            expected.lockfile,
            `package-lock.json is out of sync with package.json`,
        );
    } catch (error) {
        if (!(error instanceof assert.AssertionError)) {
            throw error;
        }

        // NPM versions <=11.10 has issues with deterministic lockfile generation,
        // especially around optional peer dependencies.
        const lines = stripVTControlCharacters(error.message)
            .split("\n")
            .map((line) => line.trim())
            .filter((line) => line.startsWith("-") || line.startsWith("+"))
            .filter((line) => !line.startsWith("+ actual - expected"))
            .filter((line) => !line.includes("peer:"));

        if (lines.length) {
            throw new Error(`Lockfile drift detected:\n${lines.join("\n")}`, { cause: error });
        }

        logger.warn(
            "Optional peer dependency differences detected. Run `npm install` to update the lockfile.",
        );
    }

    return issues;
}

run()
    .then((issues) => {
        if (issues.length) {
            logger.warn(`⚠️  ${issues.length} issue(s) detected:`);

            for (const issue of issues) {
                logger.warn(`  - ${issue}`);
            }

            if (options.warn) {
                logger.warn(
                    "The lockfile on disk has been regenerated. Review and commit the changes.",
                );
                process.exit(EXIT_DRIFT);
            }
        } else {
            logger.info("✅ Lockfile is in sync.");
        }
    })
    .catch((error) => reportAndExit(error, logger));
