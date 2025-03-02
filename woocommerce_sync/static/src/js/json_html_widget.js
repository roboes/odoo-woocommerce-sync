/** @odoo-module **/

import { registry } from "@web/core/registry";  // Keep the registry import
import { Markup } from "web.utils";  // Import Markup for HTML rendering

const { Component, useRef } = owl;

export class JsonHtmlField extends Component {
    static template = "JsonHtmlField";

    setup() {
        super.setup();
        this.input = useRef("jsonInput");
    }

    get formattedValue() {
        try {
            const value = this.props.value;
            if (!value) return "No data";

            const jsonData = typeof value === "string" ? JSON.parse(value) : value;

            // Convert JSON to a table with improved formatting
            const formattedHtml = this.formatJsonToTable(jsonData);
            return Markup(formattedHtml);  // This ensures it's rendered as raw HTML
        } catch (e) {
            return "Invalid JSON";
        }
    }

    formatJsonToTable(data) {
        const tableStyle = `
            border-collapse: collapse;
            width: 100%;
            margin-bottom: 1em;
            font-family: Arial, sans-serif;
        `;
        const thStyle = `
            background-color: #f2f2f2;
            text-align: left;
            padding: 8px;
            font-weight: bold;
        `;
        const tdStyle = `
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        `;

        if (Array.isArray(data)) {
            return `
                <table class="json-table" style="${tableStyle}">
                    <tr><th style="${thStyle}">Index</th><th style="${thStyle}">Value</th></tr>
                    ${data.map((item, index) => `
            <tr>
            <td style="${tdStyle}">${index}</td>
            <td style="${tdStyle}">${this.formatJsonToTable(item)}</td>
            </tr>`).join('')}
                </table>`;
        } else if (typeof data === "object") {
            return `
                <table class="json-table" style="${tableStyle}">
                    <tr><th style="${thStyle}">Key</th><th style="${thStyle}">Value</th></tr>
                    ${Object.entries(data).map(([key, value]) => `
            <tr>
            <td style="${tdStyle}">${key}</td>
            <td style="${tdStyle}">${this.formatJsonToTable(value)}</td>
            </tr>`).join('')}
                </table>`;
        }
        return data;
    }
}

registry.category("fields").add("json_html", JsonHtmlField);  // Ensure registration
