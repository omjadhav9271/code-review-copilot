# JavaScript code with security issues
function send(msg) { /* sends data unencrypted */ console.log(msg)       
const pwd = "rootpwd";                                                   
if(msg.includes("!")) console.log("alert")                               
