/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Markup } from "@odoo/owl";
import { Field } from "@web/views/fields/field";

export class JsonHtmlField extends Field {
    static template = "woocommerce_sync.JsonHtmlField";

    static props = {
        ...Field.props,
        value: { type: [String, Object, Array, null], optional: true },
    };

    get formattedValue() {
        try {
            const value = this.props.value;
            console.log("JsonHtmlField: Value received:", value); // Re-added for debugging
            console.log("JsonHtmlField: Type of Value:", typeof value); // Re-added for debugging

            // Handle undefined, null, empty string, and boolean false
            if (value === undefined || value === null || value === "" || (typeof value === 'boolean' && value === false)) {
                console.warn("JsonHtmlField: Value is empty, null, undefined, or boolean false.");
                return "No data";
            }

            // Attempt to parse if string, otherwise use as is
            const jsonData = typeof value === "string" ? JSON.parse(value) : value;
            console.log("JsonHtmlField: Parsed JSON data:", jsonData); // Re-added for debugging

            const formattedHtml = this.formatJsonToTable(jsonData);
            return Markup(formattedHtml);
        } catch (e) {
            console.error("JsonHtmlField: Error parsing or formatting JSON:", e);
            // Ensure error message is returned as Markup HTML
            return Markup("<p style='color: red;'>Error: Invalid JSON data or formatting error.</p>");
        }
    }

    formatJsonToTable(data) {
        const tableStyle = `
            border-collapse: collapse;
            width: 100%;
            margin-bottom: 1em;
            font-family: Arial, sans-serif;
            /* Added Odoo default table classes for better integration */
            ${Array.isArray(data) || (typeof data === "object" && data !== null) ? 'class="o_list_table table table-sm table-hover"' : ''}
        `;
        const thStyle = `
            background-color: #f2f2f2;
            text-align: left;
            padding: 8px;
            font-weight: bold;
            border: 1px solid #ddd; /* Ensure borders on headers too for consistency */
        `;
        const tdStyle = `
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
            vertical-align: top; /* Ensures content aligns nicely for multi-line */
        `;

        if (Array.isArray(data)) {
            if (data.length === 0) return "[]";


            const allKeys = [...new Set(data.flatMap(item => typeof item === 'object' && item !== null ? Object.keys(item) : []))];
            if (allKeys.length > 0) {
                return `
                    <table style="${tableStyle}">
                        <thead>
                            <tr>${allKeys.map(key => `<th style="${thStyle}">${key}</th>`).join('')}</tr>
                </thead>
                <tbody>
                ${data.map(item => `
                                <tr>
                                    ${allKeys.map(key => `<td style="${tdStyle}">${this.formatJsonToTable(item?.[key] ?? '')}</td>`).join('')}
                                </tr>`).join('')}
                </tbody>
                </table>`;
            } else {
                return `
                <table style="${tableStyle}">
                <thead>
                <tr><th style="${thStyle}">Index</th><th style="${thStyle}">Value</th></tr>
                </thead>
                <tbody>
                ${data.map((item, index) => `
                                <tr>
                                    <td style="${tdStyle}">${index}</td>
                                    <td style="${tdStyle}">${this.formatJsonToTable(item)}</td>
                                </tr>
                            `).join('')}
                </tbody>
                </table>`;
            }
        } else if (typeof data === "object" && data !== null) {
            if (Object.keys(data).length === 0) return "{}";
            return `
                <table style="${tableStyle}">
                <thead>
                <tr><th style="${thStyle}">Key</th><th style="${thStyle}">Value</th></tr>
                </thead>
                <tbody>
                ${Object.entries(data).map(([key, value]) => `
                            <tr>
                                <td style="${tdStyle}">${key}</td>
                                <td style="${tdStyle}">${this.formatJsonToTable(value)}</td>
                            </tr>
                        `).join('')}
                </tbody>
                </table>`;
            } else if (data === null) {
                return "null";
            } else if (typeof data === 'boolean') {
                return data ? 'true' : 'false';
            }
            return String(data);
        }
    }

    registry.category("fields").add("json_html", {component: JsonHtmlField,});
