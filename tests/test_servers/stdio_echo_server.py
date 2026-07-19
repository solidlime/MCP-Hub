from fastmcp import FastMCP

mcp = FastMCP("stdio-echo")


@mcp.tool
def echo(message: str) -> str:
    return f"ECHO: {message}"


if __name__ == "__main__":
    mcp.run()
